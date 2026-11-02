import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {COLORS} from '../brand';
import {EnergyFlare} from '../components/EnergyFlare';
import {ForgeBackground, Impact} from '../components/Fx';
import {TitleReveal} from '../components/TitleReveal';

export type TitleCardProps = {
	episodeNumber: number;
	episodeTitle: string;
};

/** 5-second episode title card: catalyst charge -> impact -> title -> episode name. */
export const TitleCard: React.FC<TitleCardProps> = ({episodeNumber, episodeTitle}) => {
	const frame = useCurrentFrame();
	const {durationInFrames} = useVideoConfig();
	const impact = 30;
	const fadeOut = interpolate(frame, [durationInFrames - 12, durationInFrames], [0, 1], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	const label = `EPISODE ${String(episodeNumber).padStart(2, '0')}  ·  ${episodeTitle.toUpperCase()}`;
	return (
		<AbsoluteFill style={{background: COLORS.midnight}}>
			<Impact hits={[impact]}>
				<ForgeBackground />
				<EnergyFlare burstFrame={impact} />
				<TitleReveal impactFrame={impact} episodeLabel={label} />
			</Impact>
			<AbsoluteFill style={{background: '#000', opacity: fadeOut}} />
		</AbsoluteFill>
	);
};
