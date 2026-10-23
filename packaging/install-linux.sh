#!/bin/sh
# Installs the Linux build to /opt/pharma-tnt and adds a menu entry. Run with sudo from the extracted folder.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf /opt/pharma-tnt && cp -r "$HERE/PharmaTnT" /opt/pharma-tnt
install -m 644 "$HERE/pharma-tnt.desktop" /usr/share/applications/pharma-tnt.desktop
echo "Installed. Start 'Pharma Track & Trace' from the applications menu."
