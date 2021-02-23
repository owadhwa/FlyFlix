import { RotatingCamera } from './RotatingCamera.js';

function createCamera() {
    const camera = new RotatingCamera(
        90,                                         // fov. According to measurements with the Fire tablet, this is closest to true angles at a distance of around 35mm between fly and display.
        window.innerWidth / window.innerHeight,     // aspect ratio
        0.01,                                       // near clipping
        2                                           // far clipping
    );
    return camera;
}

export { createCamera };
