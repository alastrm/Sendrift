import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f0f7ff",
          100: "#e0effe",
          200: "#bae0fd",
          300: "#7cc7fb",
          400: "#36aaf5",
          500: "#0c8ee4",
          600: "#0271c3",
          700: "#03599e",
          800: "#074c82",
          900: "#0c406d",
          950: "#082949",
        },
      },
    },
  },
  plugins: [],
};

export default config;
