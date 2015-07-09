# gdvfs - Google Drive Video File System for FUSE

When videos are uploaded to Google Drive, they are re-encoded into a variety of formats.  For example, if a 1080p high-bitrate video is uploaded to your Google Drive account, Google will make the following (lower bitrate) encodes of the original video:

  * `1080p - MP4  - h.264/aac`
  * `720p  - MP4  - h.264/aac`
  * `480p  - flv  - h.264/aac`
  * `360p  - flv  - h.264/aac`
  * `360p  - MP4  - h.264/aac`
  * `360p  - WebM - VP8/vorbis`

This project provides a FUSE file system that mounts a Google Drive account and makes these alternative encodes available via the local file system.

If the original video is named `video.mkv`, then the following files will be available:

  * `video-1080p.mp4`
  * `video-720p.mp4`
  * `video-480p.flv`
  * `video-360p.flv`
  * `video-360p.mp4`
  * `video-360p.webm`


## Installation

Run `python setup.py` to install.  You also need to have FUSE installed.

## Setup

Most of the configuration is done via a configuration file.  Look at `gdvfs.conf` for example options.