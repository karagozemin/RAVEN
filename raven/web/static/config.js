/* Runtime config for the RAVEN Control Room frontend.
 *
 * Local / single-service: leave this empty so the frontend talks to the
 * same origin that served it.
 *
 * Split deploy (Vercel frontend + Render backend): the Vercel build step
 * (scripts/build_frontend.sh) OVERWRITES this file with the Render URL taken
 * from the RAVEN_API_BASE environment variable. Do not hardcode it here. */
window.RAVEN_API_BASE = "";
