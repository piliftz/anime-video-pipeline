import gsap from 'gsap';
import {useLayoutEffect, useRef} from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';

/**
 * Drive a GSAP timeline from Remotion's frame clock.
 *
 * GSAP normally animates in real time on requestAnimationFrame. Remotion renders
 * frames out of order and in parallel browser tabs, so a "playing" GSAP timeline
 * gives different results on every render. The fix: build the timeline PAUSED and
 * seek it to `frame / fps` on every frame. That makes every frame deterministic.
 *
 * `build` receives a paused timeline. Add tweens against refs (positions in seconds).
 */
export function useGsapTimeline(build: (tl: gsap.core.Timeline) => void): void {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const tlRef = useRef<gsap.core.Timeline | null>(null);
	const buildRef = useRef(build);

	useLayoutEffect(() => {
		const tl = gsap.timeline({paused: true});
		buildRef.current(tl);
		tlRef.current = tl;
		return () => {
			tl.kill();
		};
	}, []);

	useLayoutEffect(() => {
		// suppressEvents=false keeps callbacks consistent when scrubbing backwards in the Studio.
		tlRef.current?.seek(frame / fps, false);
	}, [frame, fps]);
}
