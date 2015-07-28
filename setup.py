# Always prefer setuptools over distutils
from setuptools import setup, find_packages

# To use a consistent encoding
from codecs import open
from os import path
import re

here = path.abspath(path.dirname(__file__))

def get_version():
  return re.search("__version__ = \"([\d\.]+)\"", open("gdvfs.py").read()).groups()[0]

# Get the long description from the relevant file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='gdvfs',
    version=get_version(),
    description='A FUSE file system for Google Drive videos',
    long_description=long_description,
    url='https://github.com/wnielson/gdvfs',
    author='Weston Nielson',
    author_email='wnielson@github',
    license='MIT',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
    ],
    keywords='Google Drive FUSE file system',
    py_modules=["gdvfs"],
    install_requires=[
        'fusepy',
        'google-api-python-client'],
    package_data={
        'sample': ['gdvfs.conf'],
    },
    entry_points={
        'console_scripts': [
            'gdvfs=gdvfs:main',
        ],
    },
)
