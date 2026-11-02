import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {COLORS, FONTS} from '../brand';
import {EnergyFlare} from '../components/EnergyFlare';
import {ForgeBackground, Impact} from '../components/Fx';
import {TitleReveal} from '../components/TitleReveal';

export type CountdownReelProps = {
	daysLeft: number;
	premiereLabel: string;
	callToAction: string;
};

/** Vertical 9:16 reel for Shorts / Reels / TikTok: title, then the day count slams in. */
export const CountdownReel: React.FC<CountdownReelProps> = ({daysLeft, premiereLabel, callToAction}) => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const numberHit = 4 * fps;
	const num = spring({frame: frame - numberHit + 8, fps, config: {damping: 10, stiffness: 200, mass: 0.7}});
	const footer = interpolate(frame, [numberHit + 20, numberHit + 40], [0, 1], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	const daysText = daysLeft === 1 ? 'DAY UNTIL IGNITION' : 'DAYS UNTIL IGNITION';
	return (
		<AbsoluteFill style={{background: COLORS.midnight}}>
			<Impact hits={[30, numberHit]}>
				<ForgeBackground />
				<EnergyFlare burstFrame={numberHit} count={1400} seed="countdown" emberRatio={0.6} />
				<AbsoluteFill style={{top: -560}}>
					<TitleReveal impactFrame={30} scale={0.62} />
				</AbsoluteFill>
				<AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', top: 120}}>
					<div
						style={{
							fontFamily: FONTS.display,
							fontWeight: 900,
							fontSize: 520,
							lineHeight: 1,
							color: COLORS.white,
							opacity: num,
							transform: `scale(${3 - 2 * num})`,
							textShadow: `0 0 60px ${COLORS.cyan}, 0 0 140px ${COLORS.ember}`,
						}}
					>
						{daysLeft}
					</div>
					<div style={{fontFamily: FONTS.display, fontWeight: 700, fontSize: 58, color: COLORS.ember, letterSpacing: 6, opacity: num}}>
						{daysText}
					</div>
				</AbsoluteFill>
				<AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 220, opacity: footer}}>
					<div style={{fontFamily: FONTS.body, fontWeight: 700, fontSize: 52, color: COLORS.silver, letterSpacing: 6}}>
						{premiereLabel}
					</div>
					<div
						style={{
							marginTop: 36,
							padding: '22px 64px',
							border: `4px solid ${COLORS.cyan}`,
							borderRadius: 16,
							fontFamily: FONTS.display,
							fontWeight: 700,
							fontSize: 46,
							color: COLORS.cyan,
							letterSpacing: 8,
						}}
					>
						{callToAction}
					</div>
				</AbsoluteFill>
			</Impact>
		</AbsoluteFill>
	);
};
