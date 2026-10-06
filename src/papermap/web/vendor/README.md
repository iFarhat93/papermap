# Vendored libraries

Inlined into generated pages by `stages/render.py`, so a page keeps working offline and
from `file://`. A page only carries the libraries it needs: ELK when it has flow
diagrams, Cytoscape when it has a knowledge graph. Files are the unmodified npm builds.

| File | Package | Version | License | Used for |
|---|---|---|---|---|
| `elk.bundled.js` | [elkjs](https://github.com/kieler/elkjs) | 0.12.0 | EPL-2.0 | flow diagram layout and edge routing |
| `cytoscape.min.js` | [cytoscape](https://github.com/cytoscape/cytoscape.js) | 3.34.3 | MIT | knowledge graph |
| `layout-base.js` | [layout-base](https://github.com/iVis-at-Bilkent/layout-base) | 2.0.1 | MIT | fcose dependency |
| `cose-base.js` | [cose-base](https://github.com/iVis-at-Bilkent/cose-base) | 2.2.0 | MIT | fcose dependency |
| `cytoscape-fcose.js` | [cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) | 2.2.0 | MIT | knowledge graph layout |

License texts are in `licenses/`. To update, download with `npm pack <package>@<version>`,
copy the same file from the package, and update the table.
