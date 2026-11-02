# Vendored: the MCP Apps client

`ext-apps-app-with-deps-1.7.5.js` is `dist/src/app-with-deps.js` from the npm package
`@modelcontextprotocol/ext-apps` 1.7.5 (MIT; see `ext-apps-LICENSE.txt`), unchanged. It is the client the Prezlab
Icons panel uses to talk to Claude. `connector.py` inlines it into the panel's HTML, since a panel inside Claude
can't load scripts from anywhere. To update: copy the same file from a newer release, rename it, and change
`APPS_CLIENT` in `connector.py`; `pytest` checks the panel still builds.
