// Render settings shared by `remotion studio` and `remotion render`.
// https://www.remotion.dev/docs/config
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('png'); // lossless frames into the encoder (JPEG adds a second lossy step)
Config.setCodec('h264');
Config.setCrf(16); // matches the Python pipeline's final quality
Config.setPixelFormat('yuv420p');
Config.setColorSpace('bt709'); // correct HD color tagging, same as the main pipeline
Config.setChromiumOpenGlRenderer('angle'); // GPU WebGL for Three.js on Windows/macOS; use --gl=swangle on GPU-less Linux
Config.setOverwriteOutput(true);
