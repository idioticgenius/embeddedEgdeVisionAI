# HTML_CSS_JS_Frontend

The operator-facing web UI plus the 16 web-viewable diagram pages.

| File | Role |
|---|---|
| `index.html`              | Operator dashboard. Live MJPEG of four channels in 2x2 grid, polygon editor, recent-events list, Board Stats panel. Loaded by Flask at the `/` route. |
| `login.html`              | Flask-Login front end. Username/password form. |
| `diagrams/index.html`     | Index page for the 16 diagram pages. Renders tile grid with hover effects. |
| `diagrams/styles.css`     | Shared stylesheet. Defines the navy palette, the card layout, and the responsive grid. |
| `diagrams/diagrams.js`    | SVG helper library. Exports `D.svg`, `D.box`, `D.circle`, `D.actor`, `D.store`, `D.entity`, `D.line`. Includes gradients and drop-shadow filter defs. |
| `diagrams/01_*.html` through `diagrams/16_*.html` | One per diagram. Self-contained: load the foundation files and call the helpers inline. |

## Run locally

```sh
cd HTML_CSS_JS_Frontend
python3 -m http.server 8080
xdg-open http://localhost:8080/diagrams/index.html
```

