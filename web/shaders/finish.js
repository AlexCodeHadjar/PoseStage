// Studio finishing pass: vignette, film grain and a soft top-lit gradient that is
// applied only to dark (background) pixels, so the podium keeps its colors. With a background
// image (`backdrop` = 1) the gradient is off and the vignette is subtler.
export const FinishShader = {
  uniforms: {
    tDiffuse: { value: null },
    time: { value: 0 },
    backdrop: { value: 0 }, // 1 with a background image: no studio gradient, lighter vignette
  },

  vertexShader: /* glsl */ `
    varying vec2 vUv;

    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,

  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse;
    uniform float time;
    uniform float backdrop;
    varying vec2 vUv;

    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      vec2 p = (vUv - .5) * vec2(1.0, .85);
      float vignette = 1.0 - mix(.22, .1, backdrop) * smoothstep(.15, .72, length(p));
      float grain = fract(sin(dot(gl_FragCoord.xy + mod(time, 100.0), vec2(12.9898, 78.233))) * 43758.5453) - .5;
      // 1 for near-black studio background, 0 for lit surfaces.
      float studioMask = (1.0 - backdrop) * (1.0 - smoothstep(.06, .14, max(c.r, max(c.g, c.b))));
      vec2 spot = (vUv - vec2(.5, .65)) * vec2(3.3, 2.2);
      vec3 studio = c.rgb * mix(.78, 1.03, smoothstep(0., 1., vUv.y)) + .004 * exp(-dot(spot, spot));
      gl_FragColor = vec4(mix(c.rgb, studio, studioMask) * vignette + grain * .0011, c.a);
    }
  `,
};
