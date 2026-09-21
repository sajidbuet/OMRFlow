# Uninstalling

## Remove the application

**Settings → Apps → Installed apps → OMRFlow → Uninstall**, or run
`unins000.exe` from the installation directory.

## What is removed

| | Removed? |
|---|---|
| The application and its bundled runtime | Yes |
| The Start menu and desktop shortcuts | Yes |
| The Apps & features entry | Yes |
| **Your projects** | **No** |
| **Your settings** (`%LOCALAPPDATA%\OMRFlow\`) | **No** |
| **Application logs** (`%LOCALAPPDATA%\OMRFlow\logs\`) | **No** |

There is deliberately no "also delete my data" option. It is one careless
click, and examination data is expensive or impossible to recreate.

## Removing your data as well

Only if you are sure. With OMRFlow uninstalled:

1. Delete your project folders. **Each one contains candidate identifiers,
   recognised answers, attendance states and marks** — dispose of it the way
   your institution requires for examination records, not merely by moving it
   to the Recycle Bin.
2. Delete `%LOCALAPPDATA%\OMRFlow\` to remove settings and application logs.

## Reinstalling later

Nothing needs to be cleaned up first. Install the new version and use
**Open Project** to point it at your existing folders — see
[Moving Projects](Moving-Projects) if they are now in a different place.

## Related

- [Backup & Data Retention](Backup-and-Data-Retention)
- [Upgrading OMRFlow](Upgrading-OMRFlow)
