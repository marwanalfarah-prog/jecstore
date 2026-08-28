/**
 * Design tokens for شبيبة ستور (Part I §17).
 *
 * The palette is not a suggestion — §17.1 fixes it from the live site so a
 * returning customer still recognises the store. What this file adds is the
 * part §17.1 says was missing: proper *ramps*. The old Journal2 theme used one
 * flat shade per element, so hover, active and disabled states had nowhere to
 * go. Every brand colour here has a 50–900 scale, and components are built to
 * use 600 for rest, 700 for hover, 800 for active.
 *
 * Anchors, taken from the live site and kept exactly:
 *   primary-600   #E63950  crimson — utility bar, buttons, section headers
 *   secondary-600 #3E7CB1  steel blue — mega-menu, cart widget, steppers
 *   navy-900      #2C3446  footer
 */

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
    "./app/static/js/**/*.js",
  ],

  theme: {
    extend: {
      colors: {
        // Crimson. Primary actions, active states, discount badges.
        primary: {
          50:  "#FFF1F3",
          100: "#FFE0E5",
          200: "#FFC6CF",
          300: "#FF9DAC",
          400: "#F96A81",
          500: "#EC4A63",
          600: "#E63950", // anchor
          700: "#C42539",
          800: "#A21D2F",
          900: "#7E1826",
        },

        // Steel blue. Navigation, "New" badges, quantity steppers.
        secondary: {
          50:  "#F1F7FB",
          100: "#DFEBF5",
          200: "#BFD8EB",
          300: "#93BCDA",
          400: "#619CC6",
          500: "#4482B8",
          600: "#3E7CB1", // anchor
          700: "#32638E",
          800: "#2A5175",
          900: "#24435F",
        },

        // Dark navy/slate. Footer, and the admin panel's chrome.
        navy: {
          50:  "#F4F5F7",
          100: "#E5E7EC",
          200: "#C9CDD8",
          300: "#A3AABC",
          400: "#78819A",
          500: "#586181",
          600: "#454D68",
          700: "#3A4157",
          800: "#313A4E",
          900: "#2C3446", // anchor
          950: "#1D2331",
        },

        // Neutrals: clean and simple, per §17.1 — not richly textured.
        neutral: {
          0:   "#FFFFFF",
          50:  "#FAFAFA",
          100: "#F5F5F5", // anchor: body background
          200: "#E9E9EA",
          300: "#D6D6D8",
          400: "#A8A8AC",
          500: "#7C7C82",
          600: "#5A5A60",
          700: "#414146",
          800: "#2B2B2F",
          900: "#18181B",
        },

        // Semantic colours. §17.1 notes the old site had none beyond the
        // red/blue badge pairing, and asks for a proper set that neither
        // clashes with nor disappears against the brand colours. Green and
        // amber sit far enough from both crimson and steel blue to read as
        // status rather than brand.
        success: {
          50:  "#ECFDF3",
          100: "#D1FADF",
          600: "#15803D",
          700: "#166534",
        },
        warning: {
          50:  "#FFFAEB",
          100: "#FEF0C7",
          600: "#B45309",
          700: "#92400E",
        },
        danger: {
          50:  "#FEF2F2",
          100: "#FEE2E2",
          // Deliberately darker and browner than primary-600, so an error never
          // reads as a call to action.
          600: "#B91C1C",
          700: "#991B1B",
        },
        info: {
          50:  "#EFF6FF",
          100: "#DBEAFE",
          600: "#1D4ED8",
          700: "#1E40AF",
        },
        // Review stars (§14). Its own token rather than borrowing `warning`:
        // a five-star rating is not a caution, and the two would drift apart
        // the first time either is retuned.
        star: {
          DEFAULT: "#F0A030",
          empty:   "#E4E4E7",
        },

        // Drawn from the logo mark (§17.1) — for chart series and badge
        // variety in the admin panel, so dashboards aren't all primary red.
        accent: {
          orange: "#F08A24",
          yellow: "#F5C518",
          blue:   "#2C7BE5",
        },
      },

      fontFamily: {
        // One superfamily across both scripts. IBM Plex Sans Arabic was drawn
        // as Arabic first rather than fitted to a Latin skeleton — exactly what
        // §17.2 says to avoid — and it shares metrics and weight with IBM Plex
        // Sans, so a bilingual page holds one rhythm. Keeping to a single
        // family also keeps the font payload small, which matters when most
        // traffic arrives on mobile from a WhatsApp link (§17.5).
        sans: [
          "IBM Plex Sans Arabic",
          "IBM Plex Sans",
          "Segoe UI",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        // Numerals in tabular contexts: prices in a cart, quantities in an
        // admin table. Keeps columns from shifting as digits change.
        numeric: ["IBM Plex Sans", "IBM Plex Sans Arabic", "system-ui", "sans-serif"],
      },

      fontSize: {
        // A fixed scale, applied site-wide and in the admin panel. §17.2 calls
        // out per-page ad hoc sizing as a weakness of the old theme.
        "2xs":  ["0.6875rem", { lineHeight: "1rem" }],      // 11 — badges, meta
        xs:     ["0.75rem",   { lineHeight: "1.125rem" }],  // 12 — captions
        sm:     ["0.8125rem", { lineHeight: "1.25rem" }],   // 13 — dense UI
        base:   ["0.9375rem", { lineHeight: "1.6" }],       // 15 — body
        lg:     ["1.0625rem", { lineHeight: "1.55" }],      // 17 — lead
        xl:     ["1.25rem",   { lineHeight: "1.4" }],       // 20 — card titles
        "2xl":  ["1.5rem",    { lineHeight: "1.3" }],       // 24 — section heads
        "3xl":  ["1.875rem",  { lineHeight: "1.25" }],      // 30 — page titles
        "4xl":  ["2.25rem",   { lineHeight: "1.15" }],      // 36 — hero
        "5xl":  ["3rem",      { lineHeight: "1.05" }],      // 48 — hero, desktop
        // Price text gets its own steps: it is the single most-scanned number
        // on a listing page and should not borrow a heading size by accident.
        price:    ["1.125rem", { lineHeight: "1.2", fontWeight: "700" }],
        "price-lg": ["1.75rem", { lineHeight: "1.15", fontWeight: "700" }],
      },

      spacing: {
        // 4px base. The old theme was tightly packed; §17.4 asks for breathing
        // room, so section rhythm is built from these rather than eyeballed.
        section: "3.5rem",
        "section-lg": "5rem",
      },

      borderRadius: {
        // Soft but not pill-shaped: modern without drifting away from the
        // squared-off look customers already know.
        card: "0.625rem",
        control: "0.5rem",
      },

      boxShadow: {
        card: "0 1px 2px rgb(24 24 27 / 0.04), 0 4px 12px rgb(24 24 27 / 0.06)",
        "card-hover": "0 2px 4px rgb(24 24 27 / 0.06), 0 12px 28px rgb(24 24 27 / 0.12)",
        header: "0 1px 3px rgb(24 24 27 / 0.08)",
        dropdown: "0 8px 32px rgb(24 24 27 / 0.16)",
      },

      maxWidth: {
        content: "80rem", // 1280 — the site container
      },

      transitionDuration: {
        DEFAULT: "180ms",
      },

      keyframes: {
        // Skeleton shimmer for the loading states required by Part II §5.
        shimmer: {
          "0%":   { backgroundPosition: "200% 0" },
          "100%": { backgroundPosition: "-200% 0" },
        },
        "slide-up-fade": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s linear infinite",
        "slide-up-fade": "slide-up-fade 180ms ease-out",
      },
    },
  },

  plugins: [
    require("@tailwindcss/forms")({ strategy: "class" }),
  ],
};
