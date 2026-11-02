import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {COLORS, FONTS, SERIES} from '../brand';
import {Slash} from './Fx';

type Props = {
	/** Frame on which the last title letter lands (sync your impact frame / sound hit to this). */
	impactFrame: number;
	episodeLabel?: string;
	scale?: number;
};

/** "ARC-FORGE" slams in letter by letter, IGNITION ignites on impact, katakana settles under it. */
export const TitleReveal: React.FC<Props> = ({impactFrame, episodeLabel, scale = 1}) => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const letters = SERIES.title.split('');
	const stagger = 2;
	const firstLetter = impactFrame - (letters.length - 1) * stagger - 6;

	const ignite = spring({frame: frame - impactFrame, fps, config: {damping: 14, stiffness: 180}});
	const jp = spring({frame: frame - impactFrame - 8, fps, config: {damping: 200}});
	const label = interpolate(frame, [impactFrame + 16, impactFrame + 34], [0, 1], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	const glow = interpolate(frame, [impactFrame, impactFrame + 20], [1, 0.35], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});

	return (
		<AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', transform: `scale(${scale})`}}>
			<div style={{display: 'flex', gap: 6}}>
				{letters.map((ch, i) => {
					const s = spring({frame: frame - (firstLetter + i * stagger), fps, config: {damping: 11, stiffness: 220, mass: 0.6}});
					return (
						<span
							key={i}
							style={{
								fontFamily: FONTS.display,
								fontWeight: 900,
								fontSize: 190,
								letterSpacing: 4,
								color: COLORS.silver,
								display: 'inline-block',
								opacity: s,
								transform: `translateY(${(1 - s) * -120}px) scale(${2.4 - 1.4 * s})`,
								textShadow: `0 0 ${18 + 50 * glow}px ${COLORS.cyan}, 0 6px 0 ${COLORS.steel}`,
							}}
						>
							{ch}
						</span>
					);
				})}
			</div>
			<div
				style={{
					fontFamily: FONTS.display,
					fontWeight: 900,
					fontSize: 96,
					letterSpacing: 38 * ignite,
					color: COLORS.ember,
					marginTop: 8,
					opacity: ignite,
					transform: `scaleX(${0.6 + 0.4 * ignite})`,
					textShadow: `0 0 30px ${COLORS.ember}, 0 0 80px ${COLORS.ember}`,
				}}
			>
				{SERIES.subtitle}
			</div>
			<div
				style={{
					fontFamily: FONTS.jp,
					fontWeight: 900,
					fontSize: 44,
					color: COLORS.white,
					marginTop: 18,
					opacity: jp,
					transform: `translateY(${(1 - jp) * 24}px)`,
					letterSpacing: 6,
				}}
			>
				{SERIES.titleJa}
			</div>
			{episodeLabel && (
				<div
					style={{
						fontFamily: FONTS.body,
						fontWeight: 700,
						fontSize: 38,
						color: COLORS.cyan,
						marginTop: 40,
						letterSpacing: 8,
						opacity: label,
						clipPath: `inset(0 ${100 - label * 100}% 0 0)`,
					}}
				>
					{episodeLabel}
				</div>
			)}
			<Slash start={impactFrame - 2} y={47} />
		</AbsoluteFill>
	);
};
