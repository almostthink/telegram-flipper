/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Тёмная тема как основная — торговые интерфейсы смотрят подолгу.
        ink: {
          900: '#0b0e14',
          800: '#11151f',
          700: '#161b27',
          600: '#1e2534',
          500: '#2a3244',
        },
        accent: '#4c8dff',
        profit: '#3ecf8e',
        loss: '#ff5c5c',
        warn: '#ffb454',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
