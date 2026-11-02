// Arc-Forge: Ignition — visual identity (from the series bible).
// Fonts are bundled in public/fonts (SIL OFL, licenses alongside) so renders work offline and
// never depend on a font CDN. loadFont() blocks rendering until each font is ready.
import {loadFont} from '@remotion/fonts';
import {staticFile} from 'remotion';

export const COLORS = {
	midnight: '#070B1F', // deep midnight blue (base)
	navy: '#101A3D',
	steel: '#3A4152', // industrial gray
	silver: '#C9D1DC', // metallic silver
	ember: '#FF7A1A', // neon-orange energy
	cyan: '#19E6FF', // electric-cyan energy
	white: '#F4F7FB',
} as const;

const FONT_FILES: [family: string, file: string, weight: string][] = [
	['Orbitron', 'orbitron-latin-700-normal.woff2', '700'],
	['Orbitron', 'orbitron-latin-900-normal.woff2', '900'],
	['Rajdhani', 'rajdhani-latin-500-normal.woff2', '500'],
	['Rajdhani', 'rajdhani-latin-700-normal.woff2', '700'],
	// Kana + punctuation subset of Noto Sans JP Black. Kanji are NOT included: add them by
	// re-running the subset step described in promo/README.md.
	['Noto Sans JP Kana', 'noto-sans-jp-kana-900.woff2', '900'],
];
for (const [family, file, weight] of FONT_FILES) {
	loadFont({family, url: staticFile(`fonts/${file}`), weight});
}

export const FONTS = {
	display: 'Orbitron, sans-serif',
	body: 'Rajdhani, sans-serif',
	jp: '"Noto Sans JP Kana", sans-serif',
};

export const SERIES = {
	title: 'ARC-FORGE',
	subtitle: 'IGNITION',
	titleJa: 'アーク・フォージ：イグニッション',
} as const;
