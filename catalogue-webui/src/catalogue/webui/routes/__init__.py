"""Functional route modules for the web app.

`catalogue.webui.web.create_app` builds the Flask app, then hands it to each
module's `register(app, ctx)` to attach that area's routes. Splitting the routes
this way keeps `create_app` a thin factory instead of a 4000-line god-function,
while preserving the exact endpoint names every `url_for(...)` (in Python and in
the templates) depends on — each handler is still registered with `@app.route`
on the *same* `app`, just from its own file.

`ctx` (an `AppContext`) carries the few helpers that genuinely cross area
boundaries; everything else is local to its module.
"""
