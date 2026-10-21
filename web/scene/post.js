import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { SMAAPass } from 'three/addons/postprocessing/SMAAPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { FinishShader } from '../shaders/finish.js';

// Studio chain: scene -> subtle bloom -> vignette/grain -> SMAA -> AgX + sRGB output.
// setBackdrop(on): the finish pass switches to the background-image look (finish.js).
export function createPost(renderer, scene, camera) {
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));

  const bloom = new UnrealBloomPass(new THREE.Vector2(512, 512), 0.075, 0.35, 1.65);
  // Same additive color blend as the stock AdditiveBlending (SRC_ALPHA, ONE), but the
  // destination alpha is kept: it marks background-image pixels for the output pass.
  Object.assign(bloom.blendMaterial, {
    blending: THREE.CustomBlending,
    blendEquation: THREE.AddEquation,
    blendSrc: THREE.SrcAlphaFactor,
    blendDst: THREE.OneFactor,
    blendEquationAlpha: THREE.AddEquation,
    blendSrcAlpha: THREE.ZeroFactor,
    blendDstAlpha: THREE.OneFactor,
  });
  bloom.blendMaterial.needsUpdate = true;
  composer.addPass(bloom);

  const finish = new ShaderPass(FinishShader);
  composer.addPass(finish);

  const smaa = new SMAAPass(1, 1);
  composer.addPass(smaa);

  // Background-image pixels carry alpha 0 (stage.js backdrop): they skip tone mapping so the
  // picture keeps its colors. Studio pixels have alpha >= 1, so the studio look is unchanged.
  const output = new OutputPass();
  output.material.fragmentShader = output.material.fragmentShader.replace(
    /gl_FragColor\.rgb = (\w+ToneMapping)\( gl_FragColor\.rgb \);/g,
    'gl_FragColor.rgb = mix( gl_FragColor.rgb, $1( gl_FragColor.rgb ), clamp( gl_FragColor.a, 0.0, 1.0 ) );',
  );
  composer.addPass(output);
  const setBackdrop = (on) => {
    finish.uniforms.backdrop.value = on ? 1 : 0;
  };
  return { composer, bloom, finish, smaa, output, setBackdrop };
}
