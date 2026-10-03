# MaiBot Telegram Full

## Deploying

- Deploy with `scripts/deploy.sh` (add `--restart` when dependencies change). It copies the plugin into the
  MaiBot container on `bot` and never touches the server's `config.toml`.
- You may re-deploy whenever you want, and should do so when you finish a version; no need to ask first.
  The deployment runs the user's live production bot, so run the tests before deploying and check the
  MaiBot logs afterwards.
