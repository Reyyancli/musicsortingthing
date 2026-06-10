# THIS PYTHON SCRIPT IS ENTIRELY VIBECODED. EXPECT BUGS

## musicsortingthing

It's a script that sorts your songs based on the metadata. That's basically it. It realies heavily on correct metadata being placed, otherwise it won't work right. Doesn't connect to the internet at all, all done on system locally.

### Usage

The script works in two different ways:

1. Scan
2. Oneshot

Scanning is the default way of sorting. It checks the directory every 3 seconds for changes. If there is a new song or folder, it will sort them.

`$ ./musicsortingthing.py <directory>`

Oneshot is meant to be run on folders where theres no change to be made. If you have a folder where the songs are already there but haven't been sorted yet, this is the flag that you want to run:

`$ ./musicsortingthing.py --once <directory>`

There are two ways in which this sorts songs:

1. By album: *Album/Tracks*
2. By artist: *Artist/Album/Discs/Tracks*

By default, the script will sort by album. However, if you want to sort by artist, you will have to pass the `--hierarchy` flag. This also works with the `--once` flag

`$ ./musicsortingthing.py --hierarchy <directory>`
`$ ./musicsortingthing.py --hierarchy --once <directory>`

If you want to see what would happen if it were to run the script normally but don't want to ruin anything, you can use the `--dry-run` flag:

`$ ./musicsortingthing.py --dry-run <directory>`

Althrough i have tested a few senarios, i can't test all of them to fix. But it should work fairly well. 
