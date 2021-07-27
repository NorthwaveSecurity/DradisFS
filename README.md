<p align="center">
    <br/>
    <b>DradisFS is a <a href="https://www.kernel.org/doc/html/v5.8/filesystems/fuse.html">FUSE filesystem</a> for using the <a href="https://dradisframework.com/support/guides/rest_api/">API of Dradis</a>.</b>
    <br/>
    <a href="#goal">Goal</a>
    •
    <a href="#installation">Installation</a>
    •
    <a href="#usage">Usage</a>
    •
    <a href="#examples">Examples</a>
    <br/>
    <sub>Built with ❤ by the <a href="https://twitter.com/NorthwaveLabs">Northwave</a> Red Team</sub>
    <br/>
</p>
<hr>

# Goal

To use unix utilities and local editors for editing Dradis issues, evidences and other elements. This makes it easier to script reporting tasks. 

This project is very much a proof of concept, many features are missing or do not work as expected. 
NORTHWAVE IS NOT LIABLE FOR ANY CONSEQUENCES AS A RESULT OF USING THIS TOOL.

# Installation

```
pip install -r requirements.txt
```

Copy config.ini.example to config.ini and fill the values.

# Usage

To mount the entire Dradis filesystem, including all projects:

```
python dradisfs.py <mountpoint>
```

To mount a single project:

```
python dradisfs.py -p <project_id> <mountpoint>
```

# Examples

We start with an empty project in dradis and mount it

```
python dradisfs.py -p 433 ~/Documents/dradisfs
```

The directory looks as follows:
```
$ find .
.
./content_blocks
```

We create a new issue:
```
$ touch new
touch: new: Invalid argument
$ find .
.
./content_blocks
./113563_
./113563_/issue
$  cat 113563_/issue
#[Title]#

#[CVSSv3.BaseScore]#
#
##[CVSSv3Vector]#
#
##[Type]#
#Internal | External
#
##[Description]#
#
##[Solution]#
#
##[References]#
```

The error message can be ignored since the issue has successfully been created. Now a new directory is created with as the name the issue id and then the issue title (which is still empty). The issue directory contains the file "issue", which is the issue text. Let's give the issue a title:

```
$ cat << EOF > 113563_/issue
#[Title]#
First issue

#[CVSSv3.BaseScore]#
#
##[CVSSv3Vector]#
#
##[Type]#
#Internal | External
#
##[Description]#
#
##[Solution]#
#
##[References]#
EOF
$ find .
.
./content_blocks
./113563_First issue
./113563_First issue/issue
```

We see that the issue has been renamed using the new title. Now let's create a new node:

```
$ cd 113563_First\ issue
$ touch example.com
touch: example.com: Invalid argument
$ find .
.
./issue
./example.com
```

The node example.com has been created. Now let's create evidence for this node.

```
$ touch example.com/new
touch: example.com/new: Invalid argument
$ find .
.
./issue
./example.com
./example.com/1
$ echo -e "#[Description]#\n\nFirst evidence for node example.com" > example.com/1
$ cat example.com/1
#[Description]#

First evidence for node example.com
```

Note that you can not currently create and write an issue or evidence at the same time. So, first create a new file, then write to it. The same holds for copying files, first create a destination file, then copy.

In the same way as shown above, second and third evidences can be created:

```
$ touch evidence.com/new
touch: example.com/new: Invalid argument
$ touch evidence.com/anything
touch: example.com/anything: Invalid argument
$ find .
.
./issue
./example.com
./example.com/1
./example.com/2
./example.com/3
$ cat example.com/*
#[Description]#

First evidence for node example.com
#[Description]##[Description]#⏎
```

Now that we have these issues we can use unix tools to process them, for example `grep` and `sed`:

```
$ grep -ri evidence .

./example.com/1:First evidence for node example.com
$ for f in $(find example.com -type f); do cat $f | sed -e 's/Description/Content/' | sponge $f; end
$ cat example.com/*
#[Content]#

First evidence for node example.com
#[Content]##[Content]#⏎
```

Note that `sed -i` does not currently work, probably due to the creating of backup files.
Now to copy the contents of the first file to the second we use `cp`:

```
$ cd example.com
$ cp 1 2
$ cat 1
#[Content]#

First evidence for node example.com
$ cat 2
#[Content]#

First evidence for node example.com
$ cd ..
```

It is possible to delete evidences:

```
$ find .
.
./issue
./example.com
./example.com/1
./example.com/2
./example.com/3
$ rm example.com/1
$ find .
.
./issue
./example.com
./example.com/1
./example.com/2
```

The first evidence has been deleted, and the other evidences have been re-indexed to 1 and 2. A node can be deleted as well. Note that the node will be deleted for each issue.

```
$ rmdir example.com/
rmdir: example.com/: Invalid argument
$ find .
.
./issue
```

In the same way an issue can be deleted:

```
$ cd ..
$ find .
.
./content_blocks
./113563_First issue
./113563_First issue/issue
$ rmdir 113563_First\ issue/
rmdir: 113563_First issue/: Invalid argument
$ find .
.
./content_blocks
```

Be careful with deletion operations (including `mv`), do not use `rm -r` since a recursive deletion of an issue will delete all nodes, thereby deleting all evidences of all other issues as well.

In the same way content blocks and projects can be edited.
