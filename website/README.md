# Samsara website

This directory is the versioned source for `https://morneis.com/samsara/` and
its Docs, Compare, Business, and Support pages. The production copy lives at
`/home/morne/projects/arcana/samsara/` on the Arcana VPS.

Deploy only after the release links referenced by the pages exist. From the
repository root:

```powershell
.\website\deploy.ps1
```

The script requires the existing `the-arcana` SSH host alias. It makes a dated
server-side backup before copying files. It does not contain passwords or
disable SSH host-key verification.

The Samsara summary on the Arcana home page is part of the separate Arcana
site, not this directory. Keep it consistent when release positioning or
privacy disclosures change.
