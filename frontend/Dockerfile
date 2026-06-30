# syntax=docker/dockerfile:1.6
# ---------------------------------------------------------------------------
# TrackFundAI frontend image — nginx serving the vanilla-JS SPA.
#
# No build step needed: the app is plain HTML / CSS / JS. We copy the static
# files into the standard nginx web root and let the nginx config handle
# routing + caching.
#
# Build context: the `frontend/` directory.
#   docker build -t trackfundai-frontend .
# ---------------------------------------------------------------------------

FROM nginx:1.27-alpine

# Replace the stock nginx config with our SPA-friendly one (port 3000,
# gzip on, fallback to index.html, cache-control headers).
COPY nginx.conf /etc/nginx/nginx.conf

# Drop the default nginx welcome page.
RUN rm -rf /usr/share/nginx/html/*

# Copy every static asset that the SPA shell loads. .dockerignore keeps
# editor / OS junk out. nginx.conf survives the .dockerignore (we need it
# for the earlier COPY step) so explicitly remove it from the webroot.
COPY . /usr/share/nginx/html
RUN rm -f /usr/share/nginx/html/nginx.conf

EXPOSE 3000

# nginx:1.27-alpine ships a sensible default CMD; no override needed.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:3000/ || exit 1
