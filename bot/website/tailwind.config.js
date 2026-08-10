/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./*.html",
    "../website/templates/**/*.html",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system','BlinkMacSystemFont','SF Pro Display','SF Pro Text','Helvetica Neue','Arial','sans-serif'],
      },
      letterSpacing: {
        tighter: '-0.04em',
        tight:   '-0.02em',
      },
      transitionTimingFunction: {
        apple: 'cubic-bezier(.25,.46,.45,.94)',
      },
    },
  },
  plugins: [],
}
