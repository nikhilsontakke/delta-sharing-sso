# Quickstart

This is the only folder you need. Copy it anywhere you like, then:

```bash
pip install delta-sharing-sso[notebook]
cp .env.example .env      # fill in your Keycloak and Delta Sharing details
delta-sharing-sso-check   # sanity-check the login and list your tables
jupyter lab                # open explore_and_visualise.ipynb
```

See the [main README](https://github.com/nikhilsontakke/delta-sharing-sso#readme) for what every `.env` setting does and how to troubleshoot common errors.
