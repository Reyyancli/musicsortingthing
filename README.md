## THIS PYTHON SCRIPT IS ENTIRELY VIBECODED. EXPECT BUGS

# musicsortingthing
![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)

A python script that heavily relies on embedded metadata for sorting your music library. 

The script does **not** connect to the internet at all. Everything is done on system, completely locally. This also means your tags are what sort your music. So if your library isn't tagged well, then you will get bad results.

Below are the tags which are emphesized in this script:

- Album Name
- Album Artist
- Track Name
- Track Artist

### Requirements

- Python
- mutagen for Python

## Installation

You don't really have to install to use the script but I included it anyway to make it easier.

1. Download the latest release zip from releases and unzip it.

2. For unix based systems, run `installer.py` in a shell. If you're on windows, just double click the installer.

## Usage

**Backup your library before using this script**

The script works in two different ways:

1. Watch mode
2. Oneshot mode

### Watch Mode

Watching a directory is the default way of sorting. It checks the directory every 3 seconds for changes.

`$ ./musicsortingthing.py <directory>`

### Oneshot Mode

Runs the script and exits. This is useful for sorting libraries that isn't expected to change.

`$ ./musicsortingthing.py --once <directory>`

## Sorting Mode

There are two ways in which this sorts songs:

1. By album (default)

```
Root/
└── Album/
    └── Tracks
```

2. By artist

```
Root/
└── Artist/
    └── Album/
        └── Disc/
            └── Tracks
```

### Examples

`$ ./musicsortingthing.py --hierarchy <directory>`

`$ ./musicsortingthing.py --hierarchy --once <directory>`

## Dry run

Want to see what the script actually does without ruining your library? Try the `--dry-run` flag. 

`$ ./musicsortingthing.py --dry-run <directory>`

This will print actions without executing on them. Your library will stay as is. 

## Disclaimer

There're a few things you should acknowledge before using this:

- Metadata quality is heavily emphesized. If your songs don't have metadata at all, Things can break. Some cases like no album artist or partially matching album artists or album names have been accounted for.
- Due to the nature of the script, it can't fetch correct metadata if the embedded metadata isn't correct. It's meant for sorting your songs, not tagging them.
- Not every edge case has been tested for. I only have so much i can test, so if you find an issue while you use it, make an issue


