// Master Draft diagrams — shared SVG helper
// Each diagram page calls renderDiagram() with a spec and the SVG renders.

const D = {};

D.svg = function(w, h, contents) {
  return `<svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#12366B"/>
    </marker>
    <marker id="arrowdot" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6">
      <circle cx="5" cy="5" r="3" fill="#12366B"/>
    </marker>
    <linearGradient id="processGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#e8f4fa"/>
      <stop offset="100%" stop-color="#c4dfee"/>
    </linearGradient>
    <linearGradient id="storeGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fef9e3"/>
      <stop offset="100%" stop-color="#fde68a"/>
    </linearGradient>
    <linearGradient id="entityGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#eff6fe"/>
      <stop offset="100%" stop-color="#bfdbfe"/>
    </linearGradient>
    <linearGradient id="alertGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fef3c7"/>
      <stop offset="100%" stop-color="#fcd34d"/>
    </linearGradient>
    <filter id="dropShadow" x="-2%" y="-2%" width="104%" height="106%">
      <feGaussianBlur in="SourceAlpha" stdDeviation="1.5"/>
      <feOffset dx="0" dy="1.5" result="offsetblur"/>
      <feComponentTransfer><feFuncA type="linear" slope="0.18"/></feComponentTransfer>
      <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <style>
    text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  </style>
  ${contents}
</svg>`;
};

D.box = function(x, y, w, h, label, opts) {
  opts = opts || {};
  const fill = opts.fill || 'url(#processGrad)';
  const stroke = opts.stroke || '#12366B';
  const rx = opts.rx === undefined ? 8 : opts.rx;
  const sub = opts.sub ? `<text x="${x + w/2}" y="${y + h/2 + 16}" text-anchor="middle" font-size="11" fill="#555">${opts.sub}</text>` : '';
  return `<g filter="url(#dropShadow)">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}" stroke="${stroke}" stroke-width="1.4" rx="${rx}" ry="${rx}"/>
    <text x="${x + w/2}" y="${y + h/2 + (opts.sub ? -2 : 5)}" text-anchor="middle" font-size="13" font-weight="600" fill="#0F2A4D">${label}</text>
    ${sub}
  </g>`;
};

D.circle = function(cx, cy, r, label) {
  return `<g filter="url(#dropShadow)">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#processGrad)" stroke="#12366B" stroke-width="1.4"/>
    <text x="${cx}" y="${cy + 5}" text-anchor="middle" font-size="13" font-weight="600" fill="#0F2A4D">${label}</text>
  </g>`;
};

D.actor = function(x, y, label) {
  return `<g>
    <circle cx="${x}" cy="${y}" r="10" fill="none" stroke="#12366B" stroke-width="1.6"/>
    <line x1="${x}" y1="${y+10}" x2="${x}" y2="${y+30}" stroke="#12366B" stroke-width="1.6"/>
    <line x1="${x-12}" y1="${y+18}" x2="${x+12}" y2="${y+18}" stroke="#12366B" stroke-width="1.6"/>
    <line x1="${x}" y1="${y+30}" x2="${x-10}" y2="${y+44}" stroke="#12366B" stroke-width="1.6"/>
    <line x1="${x}" y1="${y+30}" x2="${x+10}" y2="${y+44}" stroke="#12366B" stroke-width="1.6"/>
    <text x="${x}" y="${y+62}" text-anchor="middle" font-size="13" font-weight="600" fill="#0F2A4D">${label}</text>
  </g>`;
};

D.store = function(x, y, w, label) {
  // open-ended rectangle (data store)
  return `<g filter="url(#dropShadow)">
    <rect x="${x}" y="${y}" width="${w}" height="36" fill="url(#storeGrad)" stroke="#a16207" stroke-width="1.4"/>
    <line x1="${x}" y1="${y}" x2="${x}" y2="${y+36}" stroke="#a16207" stroke-width="3"/>
    <text x="${x + w/2 + 6}" y="${y + 22}" text-anchor="middle" font-size="12" fill="#0F2A4D">${label}</text>
  </g>`;
};

D.entity = function(x, y, w, h, label) {
  return D.box(x, y, w, h, label, { fill: 'url(#entityGrad)', stroke: '#0369a1' });
};

D.line = function(x1, y1, x2, y2, label, opts) {
  opts = opts || {};
  const dash = opts.dashed ? 'stroke-dasharray="6 4"' : '';
  const arrow = opts.bidir ? 'marker-start="url(#arrow)" marker-end="url(#arrow)"' : 'marker-end="url(#arrow)"';
  let lab = '';
  if (label) {
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 4;
    lab = `<text x="${mx}" y="${my}" text-anchor="middle" font-size="11" fill="#0F2A4D" style="paint-order:stroke; stroke:white; stroke-width:3px;">${label}</text>`;
  }
  return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#12366B" stroke-width="1.5" ${dash} ${arrow}/>${lab}`;
};

D.usecase = function(cx, cy, rx, ry, label) {
  return `<g>
    <ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="#d5e8f0" stroke="#12366B" stroke-width="1.4"/>
    <text x="${cx}" y="${cy + 5}" text-anchor="middle" font-size="12" fill="#0F2A4D">${label}</text>
  </g>`;
};

if (typeof module !== 'undefined') module.exports = D;
window.D = D;
