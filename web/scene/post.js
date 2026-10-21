import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { SMAAPass } from 'three/addons/postprocessing/SMAAPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { FinishShader } from '../shaders/finish.js';

// Studio chain: scene -> subtle bloom -> vignette/grain -> SMAA -> AgX + sRGB output.
export function createPost(renderer, scene, camera) {
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));

  const bloom = new UnrealBloomPass(new THREE.Vector2(512, 512), 0.075, 0.35, 1.65);
  composer.addPass(bloom);

  const finish = new ShaderPass(FinishShader);
  composer.addPass(finish);

  const smaa = new SMAAPass(1, 1);
  composer.addPass(smaa);

  composer.addPass(new OutputPass());
  return { composer, bloom, finish, smaa };
}
