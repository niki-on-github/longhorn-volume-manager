#!/bin/bash

LOG_LEVEL=DEBUG DISABLE_SSL_VERIFICATION=True LONGHORN_URL=https://lh.server02.lan/v1 VOLUMES_CONFIG_PATH=example/volumes.yaml python3 -u volume-setup.py
