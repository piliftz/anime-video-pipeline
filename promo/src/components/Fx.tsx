import React from 'react';
import {AbsoluteFill, interpolate, random, useCurrentFrame} from 'remotion';
import {COLORS} from '../brand';

/** Deep-midnight backdrop with a slowly scrolling perspective grid and vignette. */
export const ForgeBackground: React.FC<{gridSpeed?: number}> = ({gridSpeed = 1.2}) => {
	const frame = useCurrentFrame();
	const offset = (frame * gridSpeed) % 80;
	return (
		<AbsoluteFill style={{background: `radial-gradient(ellipse at 50% 55%, ${COLORS.navy} 0%, ${COLORS.midnight} 70%)`}}>
			<AbsoluteFill
				style={{
					top: '55%',
					transform: 'perspective(600px) rotateX(62deg)',
					transformOrigin: '50% 0%',
					backgroundImage: `linear-gradient(${COLORS.cyan}22 1px, transparent 1px),
					                  linear-gradient(90deg, ${COLORS.cyan}22 1px, transparent 1px)`,
					backgroundSize: '80px 80px',
					backgroundPosition: `0 ${offset}px`,
					maskImage: 'linear-gradient(to bottom, black 0%, transparent 85%)',
				}}
			/>
			<AbsoluteFill style={{background: 'radial-gradient(ellipse at center, transparent 45%, rgba(0,0,0,0.75) 100%)'}} />
		</AbsoluteFill>
	);
};

/**
 * Anime "impact frames": at each hit frame the shot flips to a high-contrast inverted
 * black-and-white image, then a white flash, then the camera shakes and settles.
 */
export const Impact: React.FC<{hits: number[]; shake?: number; children: React.ReactNode}> = ({
	hits,
	shake = 28,
	children,
}) => {
	const frame = useCurrentFrame();
	const last = hits.filter((h) => h <= frame).pop();
	const since = last === undefined ? Infinity : frame - last;
	const inverted = since === 0;
	const flash = since === 1 ? 0.85 : since === 2 ? 0.35 : 0;
	const decay = since === Infinity ? 0 : Math.max(0, 1 - since / 12);
	const dx = (random(`sx-${frame}`) - 0.5) * 2 * shake * decay;
	const dy = (random(`sy-${frame}`) - 0.5) * 2 * shake * decay;
	return (
		<AbsoluteFill>
			<AbsoluteFill
				style={{
					transform: `translate(${dx}px, ${dy}px) scale(${1 + 0.04 * decay})`,
					filter: inverted ? 'grayscale(1) invert(1) contrast(4)' : undefined,
				}}
			>
				{children}
			</AbsoluteFill>
			{flash > 0 && <AbsoluteFill style={{background: COLORS.white, opacity: flash}} />}
		</AbsoluteFill>
	);
};

/** Diagonal energy slash that sweeps across the frame. */
export const Slash: React.FC<{start: number; duration?: number; color?: string; y?: number}> = ({
	start,
	duration = 8,
	color = COLORS.ember,
	y = 50,
}) => {
	const frame = useCurrentFrame();
	const p = interpolate(frame, [start, start + duration], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
	const fade = interpolate(frame, [start + duration, start + duration + 10], [1, 0], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	if (frame < start) return null;
	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			<div
				style={{
					position: 'absolute',
					top: `${y}%`,
					left: '-10%',
					width: `${120 * p}%`,
					height: 6,
					background: `linear-gradient(90deg, transparent, ${color}, ${COLORS.white})`,
					boxShadow: `0 0 24px ${color}, 0 0 60px ${color}`,
					transform: 'rotate(-8deg)',
					opacity: fade,
				}}
			/>
		</AbsoluteFill>
	);
};
