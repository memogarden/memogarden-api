// Console stylesheet build config — rebuild command lives in console.input.css.
// Content globs relative to this file (api/static/): the Flask templates plus
// any server-rendered HTML strings in the middleware.
module.exports = {
  content: [
    '../templates/**/*.html',
    '../middleware/*.py',
    './console.input.css',
  ],
}
