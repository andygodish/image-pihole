# First GitHub publication

Run these steps from `image-pihole` on your authenticated workstation. They
publish source and enable the established release process; they do not deploy
Pi-hole to the homelab.

1. Initialize and review the repository:

   ```sh
   git init -b main
   git add .
   git diff --cached --stat
   ```

   Confirm `.env`, `.secrets/` and backups are absent from the staged files.

2. Commit the bootstrap project:

   ```sh
   git commit -m "feat: add Pi-hole image with upver release automation"
   ```

3. Create an empty `andygodish/image-pihole` GitHub repository with your intended
   visibility. Do not initialize a remote README or push files yet. With the
   GitHub CLI, for a public repo matching the existing public image repos:

   ```sh
   gh repo create andygodish/image-pihole --public --source=. --remote=origin
   ```

4. Before the first push, open repository Settings > Actions > General and
   enable **Allow GitHub Actions to create and approve pull requests**. The
   workflow requests contents, pull-request and package write permissions.
   Enable the Renovate GitHub App for this new repository too; adding
   `renovate.json` alone does not grant the app access.

5. Push `main`:

   ```sh
   git push -u origin main
   ```

6. Check Actions. Branch CI should validate Compose and Renovate, build the
   local architecture image and run its binary/default checks. Release (upver)
   should open a release PR changing `0.0.0-0` to `2026.10.0-0` and generating
   the changelog. Review and merge that PR yourself.

7. The merge publishes both architectures at
   `ghcr.io/andygodish/pihole:v2026.10.0-0` and `:latest`, then creates the git
   tag. Confirm the first GHCR package's visibility/access allows the VM to
   pull it. GitHub package visibility is separate from repository visibility.

8. Clone or pull the repo on the Docker VM. `make build` builds the wrapper
   locally from the pinned official upstream in `Dockerfile`; its release tag
   comes from `version.txt`. `make up-local` runs that local build, while
   `make up` pulls the published image. Continue the password, test-port and
   migration steps in the README before any production cutover.

If Actions fails because repository permissions were not enabled before the
push, enable them and rerun the failed workflow. Do not manually change
`version.txt` to force the first release: the bootstrap value is intentional.
