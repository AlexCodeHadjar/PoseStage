// The exact same coverage function runs in the color and shadow-depth materials,
// so the drawing and its shadow dissolve in lockstep.
export const dissolveHeader = /* glsl */ `
uniform float uReveal;
uniform float uErase;
uniform vec2 uBounds;

float poseHash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

// Smooth value noise.
float poseNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(poseHash(i), poseHash(i + vec2(1, 0)), f.x),
             mix(poseHash(i + vec2(0, 1)), poseHash(i + vec2(1, 1)), f.x), f.y);
}
`;

// Erase sweeps a noisy front bottom-to-top from uErase; reveal grows up to uReveal.
// uBounds = texture v range of the pose bbox, so the front spans the figure only.
export const dissolveCoverage = /* glsl */ `
float poseEdge = 0.0;
// Uniform branch: an idle pose pays no procedural-noise cost at all.
if (uReveal < .99999 || uErase > .00001) {
  float poseHeight = (vMapUv.y - uBounds.x) / max(.0001, uBounds.y - uBounds.x);
  float poseField = poseHeight + (poseNoise(vMapUv * 72.0) - .5) * .075;
  float poseLow = mix(-.08, 1.08, uErase);
  float poseHigh = mix(-.08, 1.08, uReveal);
  if (poseField < poseLow || poseField > poseHigh) discard;
  poseEdge = 1.0 - smoothstep(0.0, .024, min(abs(poseField - poseLow), abs(poseField - poseHigh)));
}
`;

// Color variant also tints the dissolving edge warm white.
const edgeTint = /* glsl */ `
      diffuseColor.rgb = mix(diffuseColor.rgb, vec3(1.0, .96, .88), poseEdge * .38);
`;

export function dissolveMaterial(material, uniforms, color = false) {
  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.fragmentShader = dissolveHeader + shader.fragmentShader;
    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <alphatest_fragment>',
      '#include <alphatest_fragment>\n' + dissolveCoverage + (color ? edgeTint : ''),
    );
  };
  material.customProgramCacheKey = () => (color ? 'pose-color-r170-v2' : 'pose-depth-r170-v2');
}
