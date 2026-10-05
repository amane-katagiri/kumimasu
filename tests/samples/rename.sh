#!/bin/sh
set -eu
dir="$1"
exiftool -d '%Y%m%d-%H%M%S%%-c.%%e' '-FileName<DateTimeOriginal' "$dir"
exiftool -if 'not $DateTimeOriginal' -d '%Y%m%d-%H%M%S%%-c.%%e' '-FileName<FileModifyDate' "$dir"
