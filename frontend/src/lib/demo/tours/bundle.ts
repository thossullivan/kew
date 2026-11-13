/**
 * The bundled tours (#3145): `virtual:kew-demo-tours`, which only the demo
 * build's Vite plugin (`demo/tour-bundle.ts`) can resolve. One module reads
 * it, so a test of the controller can stand a tour set in for the bundle.
 */
import tours from 'virtual:kew-demo-tours';
import type { BundledTour } from './types';

export const TOURS: readonly BundledTour[] = tours;
