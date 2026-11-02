import React, {useRef} from 'react';
import {
	AbsoluteFill,
	getStaticFiles,
	Html5Audio,
	Img,
	interpolate,
	Series,
	staticFile,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';
import {COLORS, FONTS} from '../brand';
import {EnergyFlare} from '../components/EnergyFlare';
import {ForgeBackground, Impact} from '../components/Fx';
import {TitleReveal} from '../components/TitleReveal';
import {useGsapTimeline} from '../lib/useGsapTimeline';

export type TeaserProps = {
	/** Keyframe images inside promo/public/, e.g. "keyframes/kaito_exam.png". Missing files are skipped. */
	shots: string[];
	taglines: [string, string, string];
	zeroCards: string[];
	endCard: string;
	/** Optional music file inside promo/public/. It drops out before the final line. */
	music?: string;
};

const has = (path: string) => getStaticFiles().some((f) => f.name === path);

/** Tagline with per-letter GSAP animation, seeked from the Remotion frame clock. */
const GsapTagline: React.FC<{text: string; color?: string}> = ({text, color = COLORS.white}) => {
	const root = useRef<HTMLDivElement>(null);
	useGsapTimeline((tl) => {
		const chars = root.current?.querySelectorAll('[data-char]');
		if (!chars?.length) return;
		tl.from(chars, {opacity: 0, y: 70, rotateX: -90, duration: 0.5, ease: 'back.out(2)', stagger: 0.025});
		tl.to(chars, {opacity: 0, y: -30, duration: 0.3, ease: 'power2.in', stagger: 0.01}, '+=1.6');
	});
	return (
		<AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', perspective: 800}}>
			<div ref={root} style={{fontFamily: FONTS.display, fontWeight: 700, fontSize: 72, color, letterSpacing: 6}}>
				{text.split('').map((ch, i) => (
					<span key={i} data-char style={{display: 'inline-block', whiteSpace: 'pre'}}>
						{ch}
					</span>
				))}
			</div>
		</AbsoluteFill>
	);
};

/** One keyframe with a push-in and a hard impact on the cut. Falls back to an energy plate. */
const Shot: React.FC<{src?: string; seed: string}> = ({src, seed}) => {
	const frame = useCurrentFrame();
	const {durationInFrames} = useVideoConfig();
	const zoom = interpolate(frame, [0, durationInFrames], [1.0, 1.12]);
	return (
		<Impact hits={[0]} shake={18}>
			{src ? (
				<Img src={staticFile(src)} style={{width: '100%', height: '100%', objectFit: 'cover', transform: `scale(${zoom})`}} />
			) : (
				<>
					<ForgeBackground gridSpeed={4} />
					<EnergyFlare burstFrame={2} seed={seed} count={900} />
				</>
			)}
		</Impact>
	);
};

const ZeroCard: React.FC<{text: string}> = ({text}) => (
	<Impact hits={[0]} shake={34}>
		<AbsoluteFill style={{background: COLORS.midnight, justifyContent: 'center', alignItems: 'center'}}>
			<div style={{fontFamily: FONTS.display, fontWeight: 900, fontSize: 150, color: COLORS.ember, textShadow: `0 0 40px ${COLORS.ember}`}}>
				{text}
			</div>
		</AbsoluteFill>
	</Impact>
);

const SEC = 24;

/** 30-second teaser: hook taglines -> keyframe montage -> ZERO cards -> silence -> title -> end card. */
export const Teaser: React.FC<TeaserProps> = ({shots, taglines, zeroCards, endCard, music}) => {
	const usable = shots.filter(has);
	const shot = (i: number) => usable[i];
	const musicFile = music && has(music) ? music : undefined;
	// Sound drop: music cuts out for the final tagline (starts at 18 s) and slams back
	// in on the title impact (title sequence starts at 22.5 s, impact 30 frames later).
	const dropStart = 18 * SEC;
	const dropEnd = Math.round(22.5 * SEC) + 30;

	return (
		<AbsoluteFill style={{background: '#000'}}>
			<Series>
				<Series.Sequence durationInFrames={Math.round(3.5 * SEC)}>
					<ForgeBackground />
					<GsapTagline text={taglines[0]} />
				</Series.Sequence>
				{[0, 1].map((i) => (
					<Series.Sequence key={`a${i}`} durationInFrames={2 * SEC}>
						<Shot src={shot(i)} seed={`a${i}`} />
					</Series.Sequence>
				))}
				<Series.Sequence durationInFrames={3 * SEC}>
					<ForgeBackground />
					<GsapTagline text={taglines[1]} color={COLORS.cyan} />
				</Series.Sequence>
				{zeroCards.map((t, i) => (
					<Series.Sequence key={`z${i}`} durationInFrames={SEC}>
						<ZeroCard text={t} />
					</Series.Sequence>
				))}
				{[2, 3, 4].map((i) => (
					<Series.Sequence key={`b${i}`} durationInFrames={Math.round(1.5 * SEC)}>
						<Shot src={shot(i)} seed={`b${i}`} />
					</Series.Sequence>
				))}
				<Series.Sequence durationInFrames={Math.round(4.5 * SEC)}>
					<AbsoluteFill style={{background: '#000'}} />
					<GsapTagline text={taglines[2]} color={COLORS.ember} />
				</Series.Sequence>
				<Series.Sequence durationInFrames={5 * SEC}>
					<Impact hits={[30]}>
						<ForgeBackground />
						<EnergyFlare burstFrame={30} />
						<TitleReveal impactFrame={30} />
					</Impact>
				</Series.Sequence>
				<Series.Sequence durationInFrames={Math.round(2.5 * SEC)}>
					<AbsoluteFill style={{background: COLORS.midnight, justifyContent: 'center', alignItems: 'center'}}>
						<div style={{fontFamily: FONTS.body, fontWeight: 700, fontSize: 56, letterSpacing: 10, color: COLORS.silver}}>
							{endCard}
						</div>
					</AbsoluteFill>
				</Series.Sequence>
			</Series>
			{musicFile && (
				<Html5Audio
					src={staticFile(musicFile)}
					volume={(f) =>
						interpolate(f, [dropStart - 6, dropStart, dropEnd, dropEnd + 3], [0.8, 0, 0, 0.9], {
							extrapolateLeft: 'clamp',
							extrapolateRight: 'clamp',
						})
					}
				/>
			)}
		</AbsoluteFill>
	);
};
