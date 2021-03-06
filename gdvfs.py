#!/usr/bin/env python
"""
    gdvfs - Google Drive Video File System for FUSE
    (C) 2015 - Weston Nielson <wnielson@gmail.com>
"""
# Standard library modules
import calendar
import ConfigParser
import errno
import getopt
import logging
import os
import re
import sys
import thread
import threading
import time
import urllib
import urllib2

# Google stuff
from apiclient import errors
from apiclient.discovery import build
import httplib2
from oauth2client.client import FlowExchangeError, OAuth2WebServerFlow
from oauth2client.file import Storage
from oauth2client.tools import run_flow as run

# Fuse
import fuse

__version__ = "0.3.9"
__author__  = "Weston Nielson <wnielson@github>"

log = logging.getLogger("gdvfs")

CONFIG_SECTION  = "gdvfs"
CONFIG_DEFAULT  = {
    "include_formats":  "mp4,flv,webm",
    "include_original": "/",
    "video_extensions": "mkv,avi,mp4",
    "cache_duration":   "30",
    "root_cache":       "900",
    "mount_name":       "GDVFS",
    "debug":            "False",
    "oath_storage":     "~/.gdvfs.auth",
    "oauth_scope":      "https://www.googleapis.com/auth/drive.readonly",
    "client_id":        "356235268653-l89ucov34t3li7fg1g0rv8ppmetcgj46.apps.googleusercontent.com",
    "client_secret":    "rlls7k6VSjuM3r-nwrwq4DYv",
    "redirect_uri":     "urn:ietf:wg:oauth:2.0:oob",
    "foreground":       "False",
    "direct_io":        "True",
    "allow_other":      "False",
    "allow_root":       "False",
    "local":            "False",
    "volicon":          "",
    "lookup_threads":   "True"
}

def full_path_split(path):
    """
    Takes a path like "/a/b/c/d" and return a list like ["a", "b", "c", "d"].
    """
    segments = []
    head = path.rstrip("/")
    while True:
        head, tail = os.path.split(head)
        if not tail:
            break
        segments.append(tail)
    segments.reverse()
    return segments

def get_timestamp(string):
    """
    Returns a timestamp from a date string of the format
    '2015-07-28T16:33:27.240599Z'.  Note that we need to convert the time zone
     from "Z" (Zulu, which is UTC) to "GMT" so that `strptime` doesn't yell at
     us.
    """
    return calendar.timegm(time.strptime(string.replace("Z", "GMT"), '%Y-%m-%dT%H:%M:%S.%f%Z'))

class Node:
    """
    Represents either a folder of a file.
    """

    FOLDER_MIMETYPE = "application/vnd.google-apps.folder"
    FOLDER_BYTES    = 4096

    def __init__(self, id, title, parent, drive, video_attribs=None):
        self.id       = id
        self.title    = title
        self.parent   = parent
        self.updated  = 0
        self.attribs  = {}
        self.children = {}
        self.mtime    = 0

        self.video_attribs = video_attribs

        self._drive   = drive

    def __getitem__(self, key):
        return self.children.get(key, None)

    def get_path(self):
        paths = []
        node  = self
        while node:
            paths.insert(0, node.title)
            node = node.parent
        return os.path.join(*paths)

    def lstat(self):
        if not self.attribs:
            self.update()

        if self.video_attribs and not self.video_attribs.has_key("bytes"):
            log.debug("Getting file size for video: %s" % self.title)
            hdrs = {
                'Authorization': 'Bearer %s' % self._drive._creds.access_token,
                'Cookie': self._drive._cookies
            }
            req  = urllib2.Request(self.video_attribs.get("url"), None, hdrs)
            res  = urllib2.urlopen(req)

            self.video_attribs["bytes"] = int(res.headers.get("content-length", 0))

        if self.video_attribs and self.video_attribs.has_key("bytes"):
            bytes = self.video_attribs["bytes"]
        elif self.attribs.has_key("originalFileSize"):
            bytes = int(self.attribs.get("originalFileSize") or self.FOLDER_BYTES)
        else:
            # Directories default to 4096 bytes
            bytes = int(self.attribs.get("fileSize") or self.FOLDER_BYTES)

        return {
            "st_atime": self.mtime,
            "st_gid":   os.getgid(),        #user.pw_gid,
            "st_uid":   os.getuid(),        #user.pw_uid,
            "st_mode":  self._get_mode(),
            "st_mtime": self.mtime,
            "st_size":  bytes
        }

    def _get_mode(self):
        # From: https://github.com/thejinx0r/node-gdrive-fuse/blob/master/src/folder.coffee
        if self.attribs.get("mimeType") == self.FOLDER_MIMETYPE or self.id == "root":
            return 0o40777
        return 0o100777

    def _update_mtime(self, mtime):
        node = self
        while node != None:
            if mtime > node.mtime:
                log.debug("Updating node '%s' mtime to: %s" % (self.title, mtime))
                node.mtime = mtime
            node = node.parent

    def get_video_url(self):
        if self.video_attribs:
            return self.video_attribs.get("url", "")
        return self.attribs.get("downloadUrl", "")

    def get_children(self):
        self.update()
        return self.children

    def update(self):
        """
        Get all children of this node.
        """
        if time.time()-self.updated < self._drive.CACHE_TIME:
            # Use the cached data
            log.debug("Using cached data for node: %s" % self.title)
            return

        log.debug("Refreshing data for node: %s" % self.title)

        service     = self._drive.get_service()
        results     = []
        page_token  = None


        if self.attribs.has_key("videoMediaMetadata"):
            # This is a video, not a directory

            # Remove all videos
            self.children.clear()

            # Check for current alternate videos
            videos = self._drive.get_urls_for_docid(self.id)
            log.debug("Found %d videos for '%s'" % (len(videos), self.title))

            if len(videos) > 0:
                base_title, old_ext = os.path.splitext(self.title)

                use_threads = self._drive._config.getboolean(CONFIG_SECTION, "lookup_threads")

                # Add the alternate formats
                threads = []
                for video in videos:
                    title = "%s-%sp.%s" % (base_title, video.get("height"), video.get("extension").lower())
                    if not self.children.has_key(title):
                        # If this is a new video, add it
                        log.debug("Adding child: %s" % title)

                        # Create the new node
                        self.children[title] = Node(self.id, title, self, self._drive, video)
                        self.children[title].attribs = self.attribs.copy()
                        self.children[title]._update_mtime(get_timestamp(self.attribs.get("modifiedDate")))

                        # We need to change the mimetype and bytes so that this node
                        # appears as a video and not a directory
                        self.children[title].attribs.update({
                            "mimeType": self.attribs.get("originalMimeType")
                        })
                        self.children[title].attribs.pop("bytes", 0)

                        # XXX: Testing threaded lookups.  In my testing, this is roughly
                        #      2.5x faster than the non-threaded version.
                        if use_threads:
                            log.debug("Starting lstat thread")
                            t = threading.Thread(target=self.children[title].lstat)
                            t.start()
                            threads.append(t)

                if use_threads:
                    # Wait for all threads to finish
                    log.debug("Waiting for lstat threads")
                    for t in threads:
                      t.join()

            # See if we need to include the original video
            include_original = False
            original_include_paths = self._drive._config.get(CONFIG_SECTION, "include_original")
            if original_include_paths:
                # XXX: This could break on paths that include commas
                curr_path = self.get_path()
                for path in original_include_paths.split(","):
                    if path in curr_path:
                        include_original = True
                        break

            if include_original:
                log.debug("Adding original video")
                self.children[self.title] = Node(self.id, self.title, self, self._drive)
                self.children[self.title].attribs = self.attribs.copy()
                self.children[self.title].attribs.update({
                    "mimeType": self.attribs.get("originalMimeType"),
                    "fileSize": self.attribs.get("originalfileSize")
                })
                self.children[self.title]._update_mtime(get_timestamp(self.attribs.get("modifiedDate")))

        else:
            # This is a directory

            # Loop over pages
            while True:
                try:
                    param = {
                        "q":            "'%s' in parents and trashed=false" % self.id,
                        "fields":       "items(id,mimeType,title,createdDate,modifiedDate,fileSize,videoMediaMetadata,downloadUrl,fileExtension)",
                        "maxResults":   1000
                    }

                    if page_token:
                        param['pageToken'] = page_token

                    try:
                        files = service.files().list(**param).execute()
                    except Exception, e:
                        log.error("Error: %s" % str(e))
                        continue

                    results.extend(files['items'])

                    page_token = files.get('nextPageToken')
                    if not page_token:
                        break
                except errors.HttpError, error:
                    log.error('An error occurred: %s' % error)
                    break

            # List of titles of all children, as reported by Google
            all_children = [n['title'] for n in results]

            # Now add or update the children
            for child in results:
                if self.children.has_key(child["title"]):
                    # Update
                    self.children[child["title"]].id    = child["id"]
                    self.children[child["title"]].title = child["title"]
                else:
                    # Add
                    self.children[child["title"]] = Node(child["id"], child["title"], self, self._drive)

                node = self.children[child["title"]]

                # Set the attributes for this node
                node.attribs = child.copy()

                # Update the mtime for all children
                try:
                    node._update_mtime(get_timestamp(node.attribs.get("modifiedDate")))
                except:
                    log.error("Couldn't update mtime")

                # If the "videoMediaMetadata" key is present, then this is a video
                video_exts = [v.strip() for v in self._drive._config.get(CONFIG_SECTION, "video_extensions").lower().split(",")]
                if node.attribs.has_key("videoMediaMetadata") or node.attribs.get("fileExtension") in video_exts:
                    # Since this is a video, we need to do a few things:
                    #   (1) Change this node from a video to a directory
                    #   (2) Add this video as a child to the directory node
                    #   (3) Add alternate videos as children to the directory node

                    # Turn this video into a directory
                    node.attribs.update({
                        "originalMimeType": child.get("mimeType"),
                        "originalFileSize": child.get("fileSize"),
                        "fileSize":         self.FOLDER_BYTES,
                        "mimeType":         self.FOLDER_MIMETYPE
                    })

                else:
                    # Remove any children that are no longer present
                    for title in self.children.keys():
                        if title not in all_children:
                            log.info("Removing child: %s" % title)
                            self.children.pop(title)

        self.updated = time.time()

    def refresh_url(self):
        """
        Attempt to fetch a new stream URL for this video.  Returns ``True`` if
        one can be found, ``False`` otherwise.
        """
        match  = None

        if not self.video_attribs:
            # This is not a video
            return

        # Get an updated list of videos
        videos = self._drive.get_urls_for_docid(self.id)
        if videos and len(videos) > 0:
            # Try to find the video that matches us
            for video in videos:
                for attrib in ["extension", "width", "quality"]:
                    if self.video_attribs.get(attrib) != video.get(attrib):
                        # No match
                        match = None
                        break

                    match = video
                
                if match:
                    break

        if match:
            self.video_attribs = match
            return True

        return False


class Drive(object):
    PROTOCOL    = 'https://'

    def __init__(self, config):
        self._config    = config
        self._storage   = Storage(os.path.expanduser(config.get(CONFIG_SECTION, "oath_storage")))
        self._creds     = self._storage.get()
        self._flow      = OAuth2WebServerFlow(config.get(CONFIG_SECTION, "client_id"),
                                              config.get(CONFIG_SECTION, "client_secret"),
                                              config.get(CONFIG_SECTION, "oauth_scope"),
                                              redirect_uri=config.get(CONFIG_SECTION, "redirect_uri"))
        
        # Each thread needs to have it's own http and service instance
        self._http          = {}
        self._service       = {}

        self._tree      = Node('root', 'root', None, self)
        self._tree_lock = threading.RLock()

        # File and folder data is cache duration, in seconds
        self.CACHE_TIME = config.getint(CONFIG_SECTION, "cache_duration")

    def get_http(self):
        tid = thread.get_ident()
        if not self._http.has_key(tid):
            self._service[tid], self._http[tid] = self.build_service()
        return self._http[tid]

    def get_service(self):
        tid = thread.get_ident()
        if not self._service.has_key(tid):
            self._service[tid], self._http[tid] = self.build_service()
        return self._service[tid]


    def build_service(self, query=False):
        if self._creds is not None and self._creds.invalid:
            self._creds = run(self._flow, self._storage)

        while self._creds is None:
            print "\nGo to the following URL and copy-paste the code below:\n\n", self._flow.step1_get_authorize_url()
            try:
                self._creds = self._flow.step2_exchange(raw_input("\nCode: ").strip())
                if self._creds:
                    self._storage.put(self._creds)
            except FlowExchangeError:
                print "The code was invalid, please try again"

        # Setup HTTP
        http = httplib2.Http(timeout=5)
        self._creds.authorize(http)

        # Setup the service
        service = build('drive', 'v2', http=http)

        return service, http

    def list_dir(self, path):
        segments = full_path_split(path)
        count    = len(segments)
        listing  = {}

        # Lock
        self._tree_lock.acquire()

        parent = self._tree
        for i in range(len(segments)):
            if parent is None:
                break

            segment  = segments.pop(0)
            children = parent.get_children()
            parent   = children.get(segment)
        
        if parent:
            listing = parent.get_children()

        # Lock
        self._tree_lock.release()

        return listing

    def get_urls_for_docid(self, docid):
        params  = urllib.urlencode({'docid': docid})
        url     = self.PROTOCOL+'docs.google.com/get_video_info?docid='+str(docid)
        http    = self.get_http()

        for i in range(3):
            try:
                status, response_data = http.request(url, "GET")
                self._cookies = status['set-cookie']
                break
            except Exception, e:
                log.error("Error get_urls_for_docid: '%s' ... trying again" % str(e))
                if i == 2:
                    log.error("Error get_urls_for_docid: '%s' ... giving up" % str(e))
                    return []

        # Decode resulting player URL (URL is composed of many sub-URLs)
        urls = response_data
        urls = urllib.unquote(urllib.unquote(urllib.unquote(urllib.unquote(urllib.unquote(urls)))))
        urls = re.sub('\\\\u003d', '=', urls)
        urls = re.sub('\\\\u0026', '&', urls)

        # Do some substitutions to make anchoring the URL easier
        urls = re.sub('\&url\='+self.PROTOCOL, '\@', urls)

        itagDB      = {}
        containerDB = {
            'x-flv':'flv',
            'webm': 'WebM',
            'mp4;+codecs="avc1.42001E,+mp4a.40.2"': 'MP4'}

        for r in re.finditer('(\d+)/(\d+)x(\d+)/(\d+/\d+/\d+)\&?\,?', urls, re.DOTALL):
            (itag,resolution1,resolution2,codec) = r.groups()

            itagDB[itag] = {
                "width":  resolution1,
                "height": resolution2,
                "codec":  codec
            }

            # Rename some codecs
            if codec == '9/0/115':
                itagDB[itag]['codec'] = 'h.264/aac'
            elif codec == '99/0/0':
                itagDB[itag]['codec'] = 'VP8/vorbis'

        mediaUrls = []
        count = 0
        for r in re.finditer('\@([^\@]+)', urls):
            # XXX: Testing `rstrip("//")`
            videoURL = self.PROTOCOL+r.group(1).rstrip("//")
            for q in re.finditer('itag\=(\d+).*?type\=video\/([^\&]+)\&quality\=(\w+)', videoURL, re.DOTALL):
                (itag,container,quality) = q.groups()

                if containerDB[container].lower() not in self._config.get(CONFIG_SECTION, "include_formats").lower():
                    continue

                count = count + 1

                mediaUrls.append({
                    "quality":    quality,
                    "width":      itagDB[itag]['width'],
                    "height":     itagDB[itag]['height'],
                    "codec":      itagDB[itag]['codec'],
                    "container":  container,
                    "extension":  containerDB[container].lower(),
                    "url":        videoURL
                })
        return mediaUrls

class GDVFS(fuse.Operations):
    def __init__(self, drive):
        self.drive  = drive

        # TODO: This should be a map from a path to another dict of file handles
        #       rather than just to a single handle.  That way, we can better support
        #       having the same file opened via multiple handles.
        self.opened = {}

    # Disable unused operations
    flush       = None
    open        = None
    opendir     = None
    releasedir  = None
    statfs      = None
    chmod       = None
    chown       = None
    access      = None

    def _remove_handle(self, path):
        if self.opened.has_key(path):
            self.opened[path].close()
            self.opened.pop(path, None)

    def listxattr(self, path):
        return ["user.url", "user.cookie"]

    def getxattr(self, path, name):
        head, tail  = os.path.split(path)
        folder      = self.drive.list_dir(head)

        if folder and folder.has_key(tail):
            node = folder[tail]
            if name == "user.url":
                for i in range(3):
                    try:
                        return node.get_video_url()
                    except:
                        node.refresh_url()
            elif name == "user.cookie":
                return getattr(self.drive, '_cookies', '')
        return ""

    def read(self, path, length, offset, fh):
        log.debug("read: %s:%d -> %d +%d" % (path, fh, offset, length))

        fh   = None
        data = ""

        # Check to see if the requested path has been previously opened
        if path in self.opened:
            if self.opened[path]._pos == offset:
                # Use previously opened handle
                fh = self.opened[path]
            else:
                # Since the requested position does not match the current position,
                # a SEEK is required.  However, since we can't seek on the open
                # handle, we must first close it and then open a new one at the desired
                # offset using the "Range" header (see below)
                log.debug("Seek required, closing handle: %s" % path)
                self._remove_handle(path)

        # If we don't have an opened file, let's try to open one
        if fh is None:
            head, tail  = os.path.split(path)
            folder      = self.drive.list_dir(head)

            if folder and folder.has_key(tail):
                log.debug("Opening: %s" % path)

                node = folder[tail]

                #   a) try again, or
                #   b) check to see if a new URL needs to be generated
                for i in range(2):
                    try:
                        url = node.get_video_url()
                        hdrs = {
                            'Authorization': 'Bearer %s' % self.drive._creds.access_token,
                            'Cookie':         self.drive._cookies,
                            'Range':         'bytes=%d-' % (offset)}
                        req  = urllib2.Request(url, None, hdrs)
                        fh   = urllib2.urlopen(req)
                        break
                    except urllib2.HTTPError, e:
                        if e.code == 403:
                            # Looks like this URL is stale, we need to get a new one
                            log.info("Video URL has expired...trying to get new one")

                            node.refresh_url()

                            # Try again
                            continue
                    except Exception, e:
                        log.error("Error opening url: %s" % str(e))

                    # Give up
                    raise fuse.FuseOSError(errno.EIO)
                
                # Cache the opened URL and set the offset
                fh._pos             = offset
                self.opened[path]   = fh
            else:
                # Emulate "I/O error"
                raise fuse.FuseOSError(errno.EIO)

        try:
            # Get the data
            data = fh.read(length)
            amt  = len(data)

            log.debug("Read %d bytes" % amt)

            # Keep track of position in opened handle
            self.opened[path]._pos += amt
        except Exception, e:
            log.error("Read error: %s" % str(e))

            # TODO: Handle this read error better...
            self._remove_handle(path)

            # Emulate "I/O error"
            raise fuse.FuseOSError(errno.EIO)

        return data

    def release(self, path, fh):
        log.debug("release: %s:%d" % (path, fh))
        if path in self.opened:
            self._remove_handle(path)

    def readdir(self, path, fh):
        log.debug("readdir: %s" % path)
        return ['.', '..'] + self.drive.list_dir(path).keys()

    def getattr(self, path, fh=None):
        log.debug("getattr: %s" % path)
        
        head, tail  = os.path.split(path)
        folder      = self.drive.list_dir(head)

        if folder and folder.has_key(tail):
            # Regular file or folder
            return folder[tail].lstat()
        elif head == "/" and tail == "":
            # Root directory
            return self.drive._tree.lstat()
        
        log.debug("Unknown path: %s" % path)
        raise fuse.FuseOSError(errno.ENOENT)

def setup_logging(config, foreground):
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter())

    debug = config.getboolean(CONFIG_SECTION, "debug")

    log.setLevel(logging.DEBUG)

    if debug or foreground:
        stream_handler.setLevel(logging.DEBUG)
    else:
        stream_handler.setLevel(logging.INFO)

    log.addHandler(stream_handler)

    if config.has_option(CONFIG_SECTION, "log_path"):
        file_handler = logging.FileHandler(config.get(CONFIG_SECTION, "log_path"))
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("[%(levelname)-8s] [%(name)s] %(msg)s"))
        log.addHandler(file_handler)


def usage():
    print "Usage:\n"
    print "  %s [options]" % os.path.basename(sys.argv[0])
    print "\nOptions:\n"
    print "  -a (--auth)        : Perform OAuth authentication with Google"
    print "  -f (--foreground)  : Run in the foreground (don't daemonize)"
    print "  -c (--config=)     : Path to config file (Default: ~/.gdvfs)"
    print "  -h (--help)        : Print out this help information"
    print "\n"

def main():
    print "gdvfs version %s, Copyright (C) %s\n" % (__version__, __author__)

    try:
        opts, args = getopt.getopt(sys.argv[1:], "afhc:", ["foreground", "help", "config="])
    except getopt.GetoptError as err:
        # print help information and exit:
        print str(err) # will print something like "option -a not recognized"
        usage()
        sys.exit(2)

    config_paths = ["./gdvfs.conf", "~/.gdvfs.conf"]
    foreground   = False
    do_auth      = False

    for o, a in opts:
        if o in ("-h", "--help"):
            usage()
            sys.exit()
        elif o in ("-c", "--config"):
            config_paths.insert(0, a)
        elif o in ("-f", "--foreground"):
            foreground = True
        elif o in ("-a", "--auth"):
            do_auth = True

    config          = ConfigParser.SafeConfigParser(CONFIG_DEFAULT)
    config_paths    = [os.path.expanduser(pth) for pth in config_paths]
    loaded_configs  = config.read(config_paths)

    setup_logging(config, foreground)

    log.debug("Loaded config from: %s" % str(loaded_configs))

    drive = Drive(config)

    if do_auth:
        print "Attempting OAuth authentication"
        drive.build_service()
        return

    try:
        mount_dir = config.get(CONFIG_SECTION, "mount_dir")

        print "Mounting to: %s" % mount_dir
        if not os.path.exists(mount_dir):
            os.mkdir(mount_dir)

        kwargs = {
            "ro":               True,
            "async_read":       True,
            
            "foreground":       foreground,
            "fsname":           config.get(CONFIG_SECTION,          "mount_name"),
            "direct_io":        config.getboolean(CONFIG_SECTION,   "direct_io"),
            "allow_other":      config.getboolean(CONFIG_SECTION,   "allow_other"),
            "allow_root":       config.getboolean(CONFIG_SECTION,   "allow_root"),
        }

        if fuse.system() == "Darwin":
            kwargs.update({
                "volname":          kwargs["fsname"],
                "kill_on_unmount":  True,
                "noappledouble":    True,
                "noapplexattr":     True,
                "local":            config.getboolean(CONFIG_SECTION, "local")
            })

            volicon = config.get(CONFIG_SECTION, "volicon")
            if volicon:
                kwargs["volicon"] = volicon

        fuse.FUSE(GDVFS(drive), mount_dir, **kwargs)
    except KeyboardInterrupt:
        print "Quitting"
    except Exception, e:
        log.error("Error: %s" % str(e))

    log.info("Shutting down")
