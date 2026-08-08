<p align="center">
  <img src="vitagitDBlogolletersnobglandscape.png" alt="VitaGit Logo" width="600"/>
</p>

<p align="center">
  Homebrew database for VitaGit, a GitHub-powered homebrew store for PlayStation Vita.
</p>

---

This repository contains the database used by **VitaGit** to index and organize homebrew applications for the PlayStation Vita.

VitaGit is designed around GitHub repositories, allowing applications to be distributed directly from their developers' releases without relying on a centralized server.

## Website 

The database is used by the **VitaGit** web-based homebrew browser: [https://kiddrwxssj.github.io/vitagit.github.io/](https://kiddrwxssj.github.io/vitagit.github.io/)

## Structure

The database contains the information VitaGit needs to discover and display available homebrew applications, including their metadata and download sources.

## Contributing

- Wrong category (e.g. a game listed as a utility): edit `overrides.yml`, add a line `owner/repo: category`, and open a Pull Request.
- Missing app, or wrong info like name/version/description/dead link: open an Issue. This data comes from an external catalog and can't be fixed by editing a file here.
- Icon for an app: add a 128x128 PNG to the `icons/` folder, named exactly like that app's `icon` field in `index.yml`, and open a Pull Request.
Note: `index.yml` is auto-generated from the [NeoVitaDB-Catalog](https://github.com/robin994/NeoVitaDB-Catalog). To add a brand new app, it should also live there.

## Credits

The initial database was based on the [VitaDB](https://vitadb.rinnegatamante.it/) catalog through [NeoVitaDB](https://github.com/robin994/NeoVitaDB-Catalog), with additional references from [VitaDBtoo](https://github.com/DrDecki/VitaDBtoo-db/tree/main), followed by restructuring and additional work for VitaGit.

Homebrew icons, metadata, and other assets remain the property of their respective authors.
