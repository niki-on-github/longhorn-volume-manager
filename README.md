# Longhorn Volume Manager

Simple longhorn volume manager that is able to reuse volumes from latest backups, at cluster recreation

## Variables

- `LONGHORN_URL`: longhorn frontend URL (default `http://longhorn-frontend.longhorn-system/v1`)
- `LOG_LEVEL`: log level (default `INFO`)
- `DISABLE_SSL_VERIFICATION`: disable SSL verification (default `False`)
- `VOLUMES_CONFIG_PATH`: volume config file path (default `/config/volumes.yaml`)
- `START_DELAY_IN_SECONDS`: start delay in seconds (default `0`)
