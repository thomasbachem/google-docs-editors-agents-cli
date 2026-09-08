#!/bin/sh
# Runs the offline test suite against the tool in the parent directory.
SELF=$0
while [ -L "$SELF" ]; do
    link=$(readlink "$SELF")
    case $link in
        /*) SELF=$link ;;
        *) SELF=$(dirname -- "$SELF")/$link ;;
    esac
done
HERE=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd -P)
PY=${GAPI_PYTHON:-${GTOOLS_PYTHON:-}}
if [ -z "$PY" ]; then
    PY="$HERE/../.venv/bin/python"
    [ -x "$PY" ] || PY=python3
fi

rc=0
"$PY" "$HERE/test_gauth.py" || rc=1
echo
"$PY" "$HERE/test_auth.py" || rc=1
echo
"$PY" "$HERE/test_gsheets.py" || rc=1
echo
"$PY" "$HERE/test_gdocs.py" || rc=1
echo
"$PY" "$HERE/test_install.py" || rc=1
rm -rf "$HERE/../__pycache__" "$HERE/__pycache__"
echo
if [ $rc -eq 0 ]; then echo "all tests passed"; else echo "TESTS FAILED"; fi
exit $rc
