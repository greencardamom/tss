# -*- coding: utf-8 -*-
#
# Shared write-token resolution for the acre-side TSS tools.
# Order: explicit --token, then --token-file, then $TSS_TOKEN, then ~/.config/tss/token.
# Keeping the token in ~/.config/tss/token (chmod 600) avoids putting it on the command
# line or in crontab.

import os

TOKEN_FILE_DEFAULT = os.path.expanduser("~/.config/tss/token")


def resolve(token=None, token_file=None):
    if token:
        return token
    for path in (token_file, TOKEN_FILE_DEFAULT):
        if path and os.path.exists(path):
            with open(path) as fh:
                t = fh.read().strip()
            if t:
                return t
    return os.environ.get("TSS_TOKEN")
