# Adv. Sumit Ratnesh Pandey — professional profile

A BCI Rule 36 compliant, trilingual (English / हिंदी / मराठी) profile site. Plain HTML, CSS and vanilla JS, built with Vite.

```bash
npm install
npm run dev      # local dev server
npm run build    # production build in dist/
```


## Deployment

Every push to `claude/website-uiux-pro-max-4j3nuj` or `main` builds and deploys to GitHub Pages
(`.github/workflows/deploy.yml`): https://rohitkini34-byte.github.io/Sumit/

One-time setup: in the repo, Settings → Pages → Build and deployment → Source: **GitHub Actions**.

## Motion

Spring and orchestrated motion uses [Motion](https://motion.dev) (`motion` package): the court-door
opening, hero intro (word stagger, seal pop), arch and Sanad-card tilt (`springValue`), the Sanad flip,
the seal's scroll rotation, and the timeline/chart draws (`inView`). Everything respects
`prefers-reduced-motion`.

## Editing

- **Contact details:** `src/config.js` (`CONTACT`).
- **Portrait:** `public/portrait.jpg` (3:4, background recoloured to navy to sit inside the arch).
- **Bar Council emblem:** add `public/bar-council-logo.png` (square, transparent corners). It replaces the drawn seal in the hero and appears on the Sanad card automatically.
- **Face-follow:** drop frames in `public/face/` and list them in `FACE.frames` (row-major, `cols × rows`), or set `FACE.sprite`. It switches on automatically.
- **Text:** all copy lives in `src/i18n/{en,hi,mr}.json`.

## Structure

- `src/gate.js` — disclaimer gate, court-door opening, focus trap
- `src/hero.js` — arch tilt, lamp light, seal rotation, intro, face-follow hook
- `src/reveal.js` — timeline and SGPI chart draw-on-scroll
- `src/sanad-card.js` — enrolment flip card
- `src/i18n.js`, `src/theme.js` — language and light/dark switching
- `src/styles/` — tokens, base, gate, hero, sections, motion (incl. reduced-motion overrides)
