# Toast pre-bake analyzer

A browser tool for a bakery: upload your POS sales export, turn the knobs
(freshness window, oven size, cost, price, patience…), and see **when to
pre-bake**, **the chance each pre-baked toast sells fresh**, and **whether it
actually makes money** — with a Monte-Carlo simulation of the oven, the queue,
and customers who walk out when the wait is too long.

Everything runs **client-side in JavaScript**. No server, no backend, nothing
is uploaded anywhere — perfect for static hosting on Netlify.

---

## Run it locally

It's just static files. Any of these work:

```bash
# option A: no tools needed — just open the file
open index.html            # (double-click it)

# option B: a tiny local server (nicer; avoids file:// quirks)
python -m http.server 5173
# then visit http://localhost:5173
```

Click **“…or try sample data”** to see it work without a file.

## Deploy to Netlify (GitHub auto-deploy)

This repo is Netlify-ready — no build step.

1. Push this folder to a GitHub repo.
2. In Netlify: **Add new site → Import from GitHub**, pick the repo.
3. Build command: *(leave empty)*. Publish directory: **`.`** (root).
   `netlify.toml` already sets this.
4. Deploy. Every `git push` re-deploys automatically.

Because it's fully static, it also works on GitHub Pages, Cloudflare Pages,
Vercel, etc. — same files.

## Working in Antigravity IDE

Yes — open this folder as your project, edit, commit, push. Netlify picks up
the push and redeploys. The files you'll usually touch:

| File | What it is |
|------|------------|
| `index.html`  | Page layout + the **knob** controls (add/remove/retune sliders here). |
| `app.js`      | The whole analysis engine + UI logic. All the math lives here. |
| `styles.css`  | Styling. |
| `netlify.toml`| Deploy config (already set). |

### Adding or changing a knob
1. Add a slider in `index.html` inside `.knobs` with an `id` (e.g. `holiday_boost`)
   and a matching `<span class="val" id="holiday_boost_out">`.
2. Add that `id` to the `KNOBS` array near the top of the UI section in `app.js`.
That's it — it auto-reads the value, shows a live readout, and re-runs on change.

---

## The knobs

| Knob | Meaning |
|------|---------|
| Freshness window | Minutes a toast stays sellable. This is also the *prediction tolerance*: a pre-baked toast must be bought within this window or it's wasted. |
| Oven slots | How many toasts bake in parallel — sets whether the oven is the bottleneck. |
| Price / Cost of goods | Sets the margin (a sale) and the waste cost (a stale toast). |
| Wait before walkout | How long a customer waits before leaving = how a queue costs you money. |
| Time to order/serve | Service time per customer. |
| Bake time fastest/slowest | Range of bake times. |
| Open / Close hour | Operating hours. |
| Min fresh-sale chance | Only suggest pre-baking a toast if it sells fresh at least this often. |
| Simulation runs | More runs = steadier numbers, slightly slower. |

## The key idea

The freshness window is **not storage you can stockpile against — it's a
prediction tolerance.** A toast you start baking now is a bet that a customer
arrives in the ~2-minute window when it's ready ~7 minutes later. That bet only
pays off when demand is dense (rush) — but that's exactly when the oven is full
and has no spare slot to pre-bake into. So pre-baking is a narrow tool; for a
saturated rush the real fix is more oven capacity. The tool shows you exactly
where your shop lands.

---

## `python-tools/` (optional, not deployed)

The original Python version — useful for generating fake test data or running
the analysis from the command line. Not needed for the web app.

```bash
cd python-tools
python simulate.py --days 365 --avg 250   # make a fake year -> data/pos_orders.csv
python analyze.py --sims 200              # CLI analysis + reports/
```

Both the web app and these tools implement the same model, so results agree.
