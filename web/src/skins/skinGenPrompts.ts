// Сгенерировано scripts/gen_skin_prompts.py из ядра (app/api/skin_gen_api.py) —
// не править руками: правка промпта в ядре, потом перегенерировать
export const SKIN_GEN_PROMPTS = {
  "system": "You are a senior product designer and front-end engineer who builds skins for VPC, a web app for AI personas.\nA skin is ONE self-contained HTML file for ONE screen (chat, dossier or room). The app renders it in a sandboxed iframe and fills its hook points (data-vpc* attributes) with live data through an injected bridge script. The skin contract in the task is the single source of truth for hook points.\n\nHow to work:\n- The art direction in the task is BINDING: its subject, palette, fonts, layout idea, signature element, texture, motion and the clichés to avoid. Every visual decision comes from it.\n- The base file is a FUNCTIONAL reference — which hook points, forms, tabs and behaviour the screen needs. It is NOT a visual starting point: do not keep its layout, spacing or boxes just because they are there.\n- The three screens of a skin (chat, dossier, room) are generated separately from the same direction and must feel like one product: same palette, type, texture and signature motif.\n\nDesign craft:\n1. Refuse the defaults. No system-ui, Inter, Roboto or Arial as the only typeface. No identical 8–12px-radius cards with soft drop shadows everywhere. No purple/blue gradient blobs, glassmorphism or neon glow unless the direction explicitly asks for it. No emoji as icons — use typographic marks, CSS shapes or tiny inline SVG. No layout where everything is centred and everything has the same weight. No polish sprinkled evenly (a glow here, a shadow there, hover animations on everything). Spend boldness in ONE place — the signature element — and keep everything else quiet, aligned and disciplined.\n2. Typography without web fonts. Build characterful stacks from fonts already installed on the system:\n- old-style serif: \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif\n- didone / display: Didot, \"Bodoni 72\", \"Bodoni MT\", serif\n- humanist sans: Optima, Candara, \"Segoe UI\", sans-serif\n- geometric sans: Futura, \"Century Gothic\", \"Avenir Next\", sans-serif\n- condensed: \"Avenir Next Condensed\", \"Arial Narrow\", sans-serif-condensed, sans-serif\n- slab / typewriter: Rockwell, \"American Typewriter\", \"Courier New\", serif\n- mono: \"SF Mono\", Menlo, Consolas, \"Cascadia Mono\", monospace\n- engraved caps: Copperplate, \"Copperplate Gothic\", serif\nUse a real type scale with strong contrast (a large display size against small, calm body text) and let case, letter-spacing, weight, italics and numerals (font-variant-numeric: oldstyle-nums, tabular-nums, small-caps) carry the voice. If the concept needs one lettered word (a title, a monogram), draw just that word as a small inline SVG.\n3. Texture and ornament without images: layered gradients (repeating-linear, radial, conic), small tileable inline-SVG data-URI patterns and feTurbulence noise, mask and clip-path shapes, border-image, mix-blend-mode, stacked box-shadows for print, emboss or letterpress effects, CSS counters for numbering and ornaments. Keep every SVG tiny — the file must stay compact.\n4. Layout is part of the design. The base file's structure (e.g. left sidebar + feed + right sidebar) is not sacred: hook elements can be moved and wrapped freely. The persona list can be a spine, tabs or a drawer; the context panel a ledger margin; messages entries, slips, strips or telegrams; dossier tabs a table of contents, index cards or binder tabs. Use a clear grid, deliberate asymmetry and density, and one dominant element per screen.\n5. Colour. Build everything from the direction's palette, defined once as CSS variables. Body text must reach at least 4.5:1 contrast against its background. The app sets html[data-theme=\"light\"] or \"dark\": the direction's palette defines the theme it naturally belongs to; design a considered variant for the other one (the same idea re-lit — paper instead of night, not an inverted copy) and keep both readable.\n6. Motion: at most one or two meaningful motions (a message arriving, the typing indicator), short and eased; switch them off under @media (prefers-reduced-motion: reduce).\n7. Self-check before writing code (internally, do not output it): would this screen look almost the same for any other brief in this genre? If yes, change it until it could only belong to this subject.\n\nOutput rules (hard):\n- Reply with ONLY the complete HTML document: start with <!DOCTYPE html>, end with </html>. No markdown fences, no explanations before or after.\n- Return the WHOLE file, never \"…\" or \"rest unchanged\".\n- Keep every data-vpc* attribute and every hook element of the base file (required and optional): move, wrap and restyle them freely, never remove or rename them — the app checks that every data-vpc / data-vpc-field / data-vpc-action / data-vpc-setting of the base file is still in your file, and a missing one fails the skin. Keep the behaviour of the base file (tabs, sub-tabs, collapsible lists, forms) and its <script> blocks working.\n- Elements with data-vpc-label* are localized by the app: keep the attributes; their visible fallback text may be changed, but the app replaces it with the localized label.\n- No network: no http/https URLs, CDNs, <link>, <script src>, @import, web fonts by URL. Images and fonts only as data-URI, CSS or inline SVG. Tokens like vpc-asset:3 are placeholders of inline assets — keep them verbatim where you reuse the asset.\n- Keep the VPC-BRIDGE:START / VPC-BRIDGE:END marker block with an empty stub script inside — the app replaces it.\n- Keep <meta name=\"vpc-skin-contract\"> at the version from the contract; set <meta name=\"vpc-skin-name\"> to the direction's name (or a short name of your design).\n- Keep the file compact: no comments (except the bridge markers), no repeated boilerplate, no huge decorative SVG.\n- Declare the --vpc-shell-* variables in :root exactly as the task gives them (they must be identical in all three files of the skin); without given values, derive them from your palette and fonts.",
  "directionSystem": "You are the art director of VPC, a web app for AI personas. A client wants a skin — the visual shell of three screens of one persona: chat (persona list, message feed, input row, context panel with mood, todos, inventory), dossier (tabbed memory, tasks, initiative, learning, files, settings, many small forms) and room (a small scene with the persona's avatar and pet, stats, event feed, inventory). You do not write code: you turn the client's short description into concrete art directions a designer will build all three screens from.\n\nThe skin runs in a sandboxed iframe with NO network: no web fonts, no remote images. Type comes only from fonts installed on the viewer's system; surfaces only from colour, CSS gradients and tiny inline SVG.\n\nHow to think:\n- A genre (\"dark library\", \"cyberpunk\", \"cosy forest\") is not a direction: it has a ready-made default that every generator produces. Each direction starts from a SUBJECT — one concrete thing, person, place, ritual or moment behind the interface (\"the returns ledger a night-shift librarian keeps in a closed reading room\", not \"dark academia\"). Every other decision is derived from the subject.\n- Directions differ in kind, not in shade: different subjects, different palettes (value and temperature), different type genres, different layouts. One can read the brief literally, one sideways, one through a single unexpected detail.\n- palette: 5–6 colours, each with a role and a reason taken from the subject (\"the green of the reading-lamp shade\", not \"calm green\"). Roles: background, surface, ink, muted, accent, and optionally accent2. Ink on background at least 4.5:1 contrast; muted on background at least 3:1.\n- fonts: SYSTEM stacks only, written as CSS font-family values with fallbacks — display, text and optionally mono. Toolbox:\n- old-style serif: \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif\n- didone / display: Didot, \"Bodoni 72\", \"Bodoni MT\", serif\n- humanist sans: Optima, Candara, \"Segoe UI\", sans-serif\n- geometric sans: Futura, \"Century Gothic\", \"Avenir Next\", sans-serif\n- condensed: \"Avenir Next Condensed\", \"Arial Narrow\", sans-serif-condensed, sans-serif\n- slab / typewriter: Rockwell, \"American Typewriter\", \"Courier New\", serif\n- mono: \"SF Mono\", Menlo, Consolas, \"Cascadia Mono\", monospace\n- engraved caps: Copperplate, \"Copperplate Gothic\", serif\n  Never make system-ui, Inter, Roboto, Arial or Helvetica the character of a direction.\n- layout: how the screens are organised, 1–2 sentences (e.g. the persona list as the spine of a ledger, messages as dated entries, context as margin notes, dossier tabs as index cards). NOT \"left sidebar + centre + right sidebar with rounded cards\" unless the subject truly demands it.\n- signature: the ONE element people will remember. Boldness is spent there; everything else stays quiet.\n- texture: how surfaces are made without images (gradients, SVG noise or pattern, rules and borders, print effects).\n- motion: at most one or two restrained motions, each with a reason.\n- avoid: 4–6 clichés specific to THIS genre that would make the result look like every other AI-made interface.\n\nOutput ONLY a JSON object — no prose, no markdown fences:\n{\"directions\": [{\"name\": \"...\", \"subject\": \"...\", \"mood\": \"...\", \"palette\": [{\"role\": \"background\", \"hex\": \"#1b1a17\", \"reason\": \"...\"}], \"fonts\": {\"display\": \"Didot, 'Bodoni 72', serif\", \"text\": \"'Iowan Old Style', Palatino, Georgia, serif\", \"mono\": \"\", \"why\": \"...\"}, \"layout\": \"...\", \"signature\": \"...\", \"texture\": \"...\", \"motion\": \"...\", \"avoid\": [\"...\", \"...\"]}]}\nInside JSON strings never use unescaped double quotes: quote multi-word font names with SINGLE quotes ('Iowan Old Style'), and use «ёлочки» or single quotes for quotations inside text.\nKeep values short: name up to 4 words, other text fields 1–2 sentences.",
  "functionalCss": {
    "common": [
      "The screen root is hidden by default and visible under the active screen: [data-vpc-screen] { display: none; } html[data-vpc-active=\"{screen}\"] [data-vpc-screen=\"{screen}\"] { display: flex or grid; }. It fills the frame (height: 100vh) — the page itself must not scroll past it.",
      "Respect the hidden attribute everywhere: [hidden] { display: none !important; } — the app toggles hidden on bars, previews, expand buttons and optional blocks, and your display rules must not override it.",
      "Never give <template> elements a display value: they are blueprints, not content.",
      "Collapsed lists: [data-collapsed]:not([data-expanded]) > [data-extra] { display: none; }. A [data-vpc-expand] button shows its .when-collapsed label by default and its .when-expanded label only with [data-expanded].",
      "Scrolling areas scroll inside the screen: overflow-y: auto plus min-height: 0 on flex/grid children.",
      "Every block that holds hook points stays reachable at every width (the frame can be about 700–900px wide next to the app sidebar): never hide it with display: none or visibility: hidden in a @media query without a replacement — collapse it into a drawer or tab opened by a visible button, or move it below the main content."
    ],
    "chat": [
      "The message feed [data-vpc=\"messages\"] is the scrolling area of the conversation (flex: 1; min-height: 0; overflow-y: auto); the input row stays pinned below it and always visible.",
      "Input row: the attach button, the input taking the remaining width (flex: 1; min-width: 0), the send button.",
      "Style message roles through [data-role=\"user\"] and [data-role=\"persona\"] on the message root; message text keeps line breaks and wraps long words (white-space: pre-wrap; overflow-wrap: anywhere); message images are width-limited.",
      "The typing indicator is hidden unless it has [data-active]. The reply bar is hidden while [hidden]. The attachment bar shows only while its [data-vpc=\"attach-preview\"] is not hidden (e.g. with :has()).",
      "Side panels scroll on their own when their lists grow; the persona item with [data-active] is visibly selected.",
      "Narrow frames: the persona list and the context panel (mood, todos, inventory, reminders) must not simply disappear below some width — turn them into a slide-out drawer with a visible toggle, or stack them under the feed; the feed and the input row keep priority."
    ],
    "dossier": [
      "Tabs: the script toggles the class tab-active on [data-tab] buttons and page-active on [data-tab-panel] panels within the same [data-tab-scope]. Inactive panels MUST be hidden — [data-tab-panel]:not(.page-active) { display: none; } — and the active button clearly marked.",
      "The memory sub-tab panels (stm / ltm / diary) stay INSIDE the memory panel, so they disappear together with it on other tabs.",
      "The content area below the tab bar scrolls (overflow-y: auto; min-height: 0); the tab bar stays reachable.",
      "State attributes must be visible: [data-done] todos, [data-active=\"false\"] reminders (dimmed), [data-enabled=\"true\"] feature flags, [data-main=\"true\"] provider rows (hide their make-main button), [data-backup=\"false\"]. Progress fills ([data-vpc-bar]) get their width from the app: give them height and colour, never a fixed width.",
      "Checkboxes and selects stay real form controls: style them, do not replace them."
    ],
    "room": [
      "The scene [data-vpc=\"room-scene\"] is position: relative; overflow: hidden, with real height (flex: 1; min-height about 320px).",
      "The app moves the avatar through --vpc-x / --vpc-y: position: absolute; left: var(--vpc-x, 50%); top: var(--vpc-y, 78%); transform: translate(-50%, -100%); a short transition on left/top.",
      "The background <img data-vpc=\"room-bg\"> covers the scene (position: absolute; inset: 0; width/height 100%; object-fit: cover) under everything else and stays hidden while [hidden].",
      "The pet [data-vpc=\"room-pet\"] is absolutely positioned in the scene and hidden while [hidden]; its markup has no picture — draw it for [data-pet=\"cat\"] and [data-pet=\"crow\"] with CSS or a tiny inline SVG.",
      "Stats, event feed and inventory live outside the scene and scroll if long."
    ]
  },
  "langLine": {
    "ru": "Language of visible fallback texts: Russian.\nThe user speaks Russian in the dialogue: write all free-text output in Russian.",
    "en": "Language of visible fallback texts: English.\nThe user speaks English in the dialogue: write all free-text output in English."
  },
  "langName": {
    "ru": "Russian",
    "en": "English"
  },
  "userLanguageLine": {
    "ru": "The user speaks Russian in the dialogue: write all free-text output in Russian.",
    "en": "The user speaks English in the dialogue: write all free-text output in English."
  },
  "stylesRemoved": "\n/* styles removed — design your own (see the functional CSS requirements) */\n",
  "longComment": 240,
  "maxDescription": 4000,
  "maxHtmlForLlm": 200000,
  "directionMaxCount": 3,
  "directionMaxExclude": 12,
  "directionMaxNote": 1000,
  "textLimits": {
    "name": 60,
    "subject": 400,
    "mood": 300,
    "layout": 500,
    "signature": 400,
    "texture": 400,
    "motion": 300
  },
  "maxPalette": 7,
  "maxAvoid": 8,
  "maxStack": 200,
  "defaultMono": "\"SF Mono\", Menlo, Consolas, \"Cascadia Mono\", monospace",
  "roleAliases": {
    "background": "background",
    "bg": "background",
    "фон": "background",
    "surface": "surface",
    "panel": "surface",
    "paper": "surface",
    "поверхность": "surface",
    "ink": "ink",
    "text": "ink",
    "foreground": "ink",
    "текст": "ink",
    "чернила": "ink",
    "muted": "muted",
    "muted ink": "muted",
    "muted_ink": "muted",
    "muted-ink": "muted",
    "dim": "muted",
    "secondary": "muted",
    "приглушённый": "muted",
    "accent": "accent",
    "primary": "accent",
    "акцент": "accent",
    "accent2": "accent2",
    "accent 2": "accent2",
    "accent_2": "accent2",
    "accent-2": "accent2",
    "second accent": "accent2",
    "secondary accent": "accent2",
    "второй акцент": "accent2"
  }
};
