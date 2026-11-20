// Console stylesheet build config — rebuild command lives in console.input.css.
// Content globs relative to this file (api/static/): the Flask templates plus
// any server-rendered HTML strings in the middleware.
module.exports = {
  // Class strategy: the console theme toggle (console_base.html) sets/removes
  // `dark` on <html>; localStorage 'theme' = light | dark | auto (default,
  // follows prefers-color-scheme via the base.html head bootstrap).
  darkMode: 'class',
  content: [
    '../templates/**/*.html',
    '../middleware/*.py',
    './console.input.css',
  ],
}
