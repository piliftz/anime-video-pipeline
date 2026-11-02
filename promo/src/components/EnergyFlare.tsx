import {ThreeCanvas} from '@remotion/three';
import React, {useMemo} from 'react';
import {AdditiveBlending, CanvasTexture, Color} from 'three';
import {interpolate, random, useCurrentFrame, useVideoConfig} from 'remotion';
import {COLORS} from '../brand';

type FlareProps = {
	/** Frame at which the catalyst "ignites" (particles burst outward). */
	burstFrame?: number;
	count?: number;
	seed?: string;
	/** 0..1 share of particles that are ember-orange (the rest are electric cyan). */
	emberRatio?: number;
};

// A soft round sprite so points render as glowing dots instead of squares.
function useGlowTexture(): CanvasTexture {
	return useMemo(() => {
		const size = 64;
		const canvas = document.createElement('canvas');
		canvas.width = size;
		canvas.height = size;
		const ctx = canvas.getContext('2d')!;
		const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
		g.addColorStop(0, 'rgba(255,255,255,1)');
		g.addColorStop(0.35, 'rgba(255,255,255,0.55)');
		g.addColorStop(1, 'rgba(255,255,255,0)');
		ctx.fillStyle = g;
		ctx.fillRect(0, 0, size, size);
		return new CanvasTexture(canvas);
	}, []);
}

/**
 * Catalyst energy particles. Every position is a pure function of (frame, seed):
 * no state is carried between frames, so parallel/out-of-order rendering is exact.
 */
const Particles: React.FC<Required<FlareProps>> = ({burstFrame, count, seed, emberRatio}) => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const texture = useGlowTexture();

	const particles = useMemo(
		() =>
			Array.from({length: count}, (_, i) => ({
				angle: random(`${seed}-a-${i}`) * Math.PI * 2,
				speed: 0.6 + random(`${seed}-s-${i}`) * 3.4,
				lift: (random(`${seed}-h-${i}`) - 0.5) * 1.6,
				spin: (random(`${seed}-w-${i}`) - 0.5) * 2.2,
				orbit: 0.4 + random(`${seed}-o-${i}`) * 1.6,
				ember: random(`${seed}-c-${i}`) < emberRatio,
			})),
		[count, seed, emberRatio],
	);

	const colors = useMemo(() => {
		const cyan = new Color(COLORS.cyan);
		const ember = new Color(COLORS.ember);
		const arr = new Float32Array(count * 3);
		particles.forEach((p, i) => {
			const c = p.ember ? ember : cyan;
			arr.set([c.r, c.g, c.b], i * 3);
		});
		return arr;
	}, [particles, count]);

	const positions = useMemo(() => {
		const t = frame / fps;
		const sinceBurst = Math.max(0, (frame - burstFrame) / fps);
		const arr = new Float32Array(count * 3);
		particles.forEach((p, i) => {
			// Before the burst: a slow charging orbit. After: an outward spiral that decelerates.
			const charge = p.orbit * (1 - Math.min(1, sinceBurst * 4));
			const travel = p.speed * (1 - Math.exp(-sinceBurst * 2.2));
			const r = charge + travel;
			const a = p.angle + t * p.spin;
			arr[i * 3] = Math.cos(a) * r;
			arr[i * 3 + 1] = Math.sin(a) * r * 0.62 + p.lift * travel * 0.35;
			arr[i * 3 + 2] = Math.sin(a * 0.5) * 0.6;
		});
		return arr;
	}, [frame, fps, burstFrame, count, particles]);

	const opacity = interpolate(frame, [burstFrame - 6, burstFrame, burstFrame + 2.5 * fps], [0.55, 1, 0.35], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});

	return (
		<points>
			<bufferGeometry>
				{/* `key={frame}` re-creates the attribute each frame so three.js uploads the new positions. */}
				<bufferAttribute key={frame} attach="attributes-position" args={[positions, 3]} />
				<bufferAttribute attach="attributes-color" args={[colors, 3]} />
			</bufferGeometry>
			<pointsMaterial
				size={0.09}
				map={texture}
				vertexColors
				transparent
				opacity={opacity}
				depthWrite={false}
				blending={AdditiveBlending}
				sizeAttenuation
			/>
		</points>
	);
};

export const EnergyFlare: React.FC<FlareProps> = ({
	burstFrame = 24,
	count = 1800,
	seed = 'catalyst',
	emberRatio = 0.4,
}) => {
	const {width, height} = useVideoConfig();
	return (
		<ThreeCanvas width={width} height={height} camera={{position: [0, 0, 6], fov: 50}} gl={{alpha: true}}>
			<Particles burstFrame={burstFrame} count={count} seed={seed} emberRatio={emberRatio} />
		</ThreeCanvas>
	);
};
