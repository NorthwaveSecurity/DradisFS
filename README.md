<p align="center">
    <br/>
    <b>DradisFS is a <a href="https://www.kernel.org/doc/html/v5.8/filesystems/fuse.html">FUSE filesystem</a> for using the <a href="https://dradisframework.com/support/guides/rest_api/">API of Dradis</a>.</b>
    <br/>
    <a href="#goal">Goal</a>
    •
    <a href="#installation">Installation</a>
    •
    <a href="#usage">Usage</a>
    <br/>
    <sub>Built with ❤ by the <a href="https://twitter.com/NorthwaveLabs">Northwave</a> Red Team</sub>
    <br/>
</p>
<hr>

# Goal

To use unix utilities and local editors for editing Dradis issues, evidences and other elements. This makes it easier to script reporting tasks. 

This project is very much a proof of concept, many features are missing or do not work as expected. 
NORTHWAVE IS NOT LIABLE FOR ANY CONSEQUENCES AS A RESULT OF USING THIS TOOL

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
