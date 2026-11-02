import React from 'react';
import {Composition} from 'remotion';
import {CountdownReel, type CountdownReelProps} from './compositions/CountdownReel';
import {Teaser, type TeaserProps} from './compositions/Teaser';
import {TitleCard, type TitleCardProps} from './compositions/TitleCard';

// 24 fps everywhere to match the episode pipeline (anime is animated on 24/12/8 fps).
const FPS = 24;

export const RemotionRoot: React.FC = () => (
	<>
		<Composition
			id="TitleCard"
			component={TitleCard}
			durationInFrames={5 * FPS}
			fps={FPS}
			width={1920}
			height={1080}
			defaultProps={{episodeNumber: 1, episodeTitle: 'The Zero-Catalyst Drop'} satisfies TitleCardProps}
		/>
		<Composition
			id="Teaser"
			component={Teaser}
			durationInFrames={30 * FPS}
			fps={FPS}
			width={1920}
			height={1080}
			defaultProps={
				{
					shots: [
						'keyframes/shot_01.png',
						'keyframes/shot_02.png',
						'keyframes/shot_03.png',
						'keyframes/shot_04.png',
						'keyframes/shot_05.png',
					],
					taglines: ['THE ACADEMY RANKS EVERY SPARK.', 'ONE STUDENT HAS NONE.', 'UNTIL HE TOUCHED THE WILDFIRE.'],
					zeroCards: ['ZERO POWER.', 'ZERO RANK.', 'ZERO CHANCE.'],
					endCard: 'PILOT  ·  COMING SOON',
					music: 'audio/teaser_music.wav',
				} satisfies TeaserProps
			}
		/>
		<Composition
			id="CountdownReel"
			component={CountdownReel}
			durationInFrames={15 * FPS}
			fps={FPS}
			width={1080}
			height={1920}
			defaultProps={
				{daysLeft: 7, premiereLabel: 'EPISODE 01  ·  THE ZERO-CATALYST DROP', callToAction: 'SUBSCRIBE'} satisfies CountdownReelProps
			}
		/>
	</>
);
