# Samsara website

This directory is the versioned source for `https://morneis.com/samsara/`
and its Docs, Compare, Business, and Support pages.

Deploy after the release links referenced by the pages exist. Supply your
configured SSH host, remote parent directory, and server ownership explicitly:

```powershell
.\website\deploy.ps1 -SshHost example-host -RemoteRoot /srv/www -RemoteOwner www-data:www-data
```

The script makes a dated server-side backup before copying files. It uses
your SSH configuration and preserves host-key verification.
