- 写真の名前を撮影日時にそろえたい。スマホとデジカメで命名がばらばら（IMG_1234.JPG と DSC01234.JPG）
- EXIF の DateTimeOriginal を読めば撮影日時が分かる。exiftool で読める https://exiftool.org/
- `exiftool -d '%Y%m%d-%H%M%S%%-c.%%e' '-FileName<DateTimeOriginal' DIR` で一括リネームできる
- 試したら、スマホの写真 312 枚のうち 9 枚に DateTimeOriginal が無かった。LINE で受け取った写真だった
- LINE 経由の写真は EXIF が消えている。ファイルの更新日時で代用するしかない
- 同じ秒に連写した写真は名前がぶつかる。`%-c` を付けると -1, -2 と連番になる
- タイムゾーンが入っていないので、海外で撮った写真は現地時刻のまま並ぶ。気にしないことにした
- 最初は Python で書こうとしたが、exiftool 1 行で済んだので書くのをやめた
- 参考: https://exiftool.org/filename.html

EXIF の無い写真をまとめる手順のメモ。

```sh
exiftool -if 'not $DateTimeOriginal' -p '$FileName' DIR > no-exif.txt
```
