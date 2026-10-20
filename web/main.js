import * as THREE from 'three';
import {createStage,frameCamera} from './scene/stage.js';
import {createPost} from './scene/post.js';
import {Figure} from './scene/figure.js';
import {TextureCache} from './scene/textures.js';

const canvas=document.querySelector('#stage'),errorPanel=document.querySelector('#error');
let api,renderer,studio,post,camera,figureCamera,overlay,framing,cache,poses=[],current=null,index=0;
let transition=null,loading=false,pending=0,active=document.hasFocus(),frameId=0,lastFrame=0;
let mouse={x:0,y:0,time:-10000},idleTimer,angle=0,frameCount=0,sampleStart=0,fps=60,quality=0;
let firstPoseMs=null, started=performance.now(),renderedFrames=0;
const lastShadowCamera=new THREE.Vector3(Infinity,Infinity,Infinity);
const clamp=(x,a=0,b=1)=>Math.min(b,Math.max(a,x));
const ease=x=>{x=clamp(x);return x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2;};
const report=message=>{console.error(message);api?.report_error(String(message));};

function error(message,choose=false) {
  document.querySelector('#message').textContent=message;
  document.querySelector('#choose').hidden=!choose;errorPanel.hidden=false;document.body.classList.add('error');
}
function clearError(){errorPanel.hidden=true;document.body.classList.remove('error');}
function wake(){document.body.classList.remove('idle');clearTimeout(idleTimer);idleTimer=setTimeout(()=>document.body.classList.add('idle'),2500);}

function resize() {
  if(!renderer)return;
  const w=innerWidth,h=innerHeight;
  // Keep the final figure at native DPI even when the studio adapts its resolution.
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  renderer.setSize(w,h,false);
  const studioRatio=Math.min(renderer.getPixelRatio(),Math.sqrt(1920*1080/(w*h)))*Math.pow(.8,quality);
  post.composer.setPixelRatio(studioRatio);post.composer.setSize(w,h);
  framing=frameCamera(camera,w,h);
  figureCamera.left=-w/2;figureCamera.right=w/2;figureCamera.top=h/2;figureCamera.bottom=-h/2;figureCamera.updateProjectionMatrix();
  current?.fit(camera);transition?.next.fit(camera);renderer.shadowMap.needsUpdate=true;
}

async function firstPose(list) {
  poses=list;
  if(!poses.length){error('Позы не найдены',true);return;}
  clearError();
  const state=await api.get_state();index=clamp(Number.isInteger(state.index)?state.index:0,0,poses.length-1);
  cache=new TextureCache(poses,renderer,report);
  for(let tries=0;tries<poses.length;tries++) {
    try {
      cache.pins=new Set([index]);const texture=await cache.get(index);
      current=new Figure(poses[index],texture,studio.scene,overlay);current.fit(camera);current.face(camera);
      renderer.shadowMap.needsUpdate=true;cache.preload(index);firstPoseMs=performance.now()-started;api.save_state(index);return;
    }catch(e){report(e);index=(index+1)%poses.length;}
  }
  error('Позы не найдены',true);
}

async function begin(direction) {
  if(!current||!poses.length)return;
  loading=true;let target=index,texture=null;
  for(let tries=0;tries<poses.length;tries++) {
    target=(target+direction+poses.length)%poses.length;
    cache.pins=new Set([index,target]);
    try{texture=await cache.get(target);break;}catch(e){report(e);}
  }
  loading=false;
  if(!texture){pending=0;cache.pins=new Set([index]);return;}
  if(target===index){pending=0;cache.pins=new Set([index]);return;}
  const next=new Figure(poses[target],texture,studio.scene,overlay);next.fit(camera);next.face(camera);next.uniforms.uReveal.value=0;
  transition={old:current,next,target,time:0,speed:pending?8:1,from:angle,to:angle+direction*Math.PI/4};
  renderer.shadowMap.needsUpdate=true;
  if(!active)finish();
}
function requestStep(direction) {
  wake();
  if(loading||transition){pending=direction;if(transition)transition.speed=8;return;}
  begin(direction).catch(report);
}
function finish() {
  if(!transition)return;
  const t=transition;angle=t.to;studio.podium.rotation.y=angle;
  t.old.dispose();current=t.next;current.uniforms.uReveal.value=1;current.uniforms.uErase.value=0;index=t.target;
  transition=null;studio.contact.material.opacity=1;cache.pins=new Set([index]);cache.preload(index);
  renderer.shadowMap.needsUpdate=true;api.save_state(index);
  const step=pending;pending=0;if(step&&active)begin(step).catch(report);
}

function renderFrame(now) {
  const dt=lastFrame?clamp((now-lastFrame)/1000,0,.06):1/60;lastFrame=now;
  const pointerAlive=now-mouse.time<450;
  const x=pointerAlive?mouse.x:0,y=pointerAlive?mouse.y:0;
  const breath=Math.sin(now/1450)*.002;
  const wantedX=framing.base.x+framing.distance*(x*Math.sin(THREE.MathUtils.degToRad(4))+breath);
  const wantedY=framing.base.y+framing.distance*(y*.006+Math.cos(now/1600)*.001);
  const smooth=1-Math.exp(-dt*3);
  camera.position.x=THREE.MathUtils.lerp(camera.position.x,wantedX,smooth);
  camera.position.y=THREE.MathUtils.lerp(camera.position.y,wantedY,smooth);
  camera.lookAt(framing.target);camera.updateMatrixWorld();
  current?.face(camera);transition?.next.face(camera);
  if(camera.position.distanceToSquared(lastShadowCamera)>Math.pow(framing.distance*.0003,2)) {
    renderer.shadowMap.needsUpdate=true;lastShadowCamera.copy(camera.position);
  }
  if(transition) {
    const t=transition;t.time+=dt*t.speed;
    const progress=clamp(t.time/.66);
    t.old.uniforms.uErase.value=ease(progress/.65);
    t.next.uniforms.uReveal.value=ease((progress-.25)/.75);
    studio.contact.material.opacity=Math.max(1-t.old.uniforms.uErase.value,t.next.uniforms.uReveal.value);
    studio.podium.rotation.y=THREE.MathUtils.lerp(t.from,t.to,ease(progress));
    renderer.shadowMap.needsUpdate=true;if(progress>=1)finish();
  }
  post.finish.uniforms.time.value=now/1000;
  renderer.info.autoReset=false;renderer.info.reset();
  post.composer.render(dt);
  // AgX, bloom, film grain, vignette and SMAA have ALREADY run. This final unlit
  // sRGB pass never sees those filters. clearDepth leaves the studio color intact.
  renderer.setRenderTarget(null);renderer.clearDepth();renderer.render(overlay,figureCamera);
  renderedFrames++;
}

function draw(now) {
  frameId=0;if(!active||document.hidden)return;
  renderFrame(now);
  frameCount++;
  if(!sampleStart)sampleStart=now;
  if(now-sampleStart>=2500) {
    fps=frameCount*1000/(now-sampleStart);frameCount=0;sampleStart=now;
    if(firstPoseMs!==null&&now-started>6000&&fps<53&&quality<3) {
      post.bloom.enabled=false;quality++;resize();
    }
  }
  frameId=requestAnimationFrame(draw);
}
function resume(){active=true;lastFrame=0;sampleStart=0;frameCount=0;if(!frameId)frameId=requestAnimationFrame(draw);}
function pause(){active=false;cancelAnimationFrame(frameId);frameId=0;pending=0;finish();lastFrame=0;}

async function start() {
  try {
    renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:false,powerPreference:'high-performance'});
  }catch(e){error('WebGL недоступен. Включите аппаратное ускорение и обновите драйвер видеокарты.');throw e;}
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.AgXToneMapping;renderer.toneMappingExposure=1;
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;renderer.shadowMap.autoUpdate=false;renderer.autoClear=false;
  renderer.setClearColor('#16181b',1);
  camera=new THREE.PerspectiveCamera(30,1,.05,250);overlay=new THREE.Scene();
  figureCamera=new THREE.OrthographicCamera(-1,1,1,-1,.1,20);figureCamera.position.z=10;
  studio=createStage(renderer);post=createPost(renderer,studio.scene,camera);resize();
  if(active)resume();
  canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();pause();error('Графический контекст потерян. Перезапустите PoseStage.');report('WebGL context lost');});
  api=await new Promise(resolve=>{
    if(window.pywebview?.api)resolve(window.pywebview.api);
    else window.addEventListener('pywebviewready',()=>resolve(window.pywebview.api),{once:true});
  });
  let list=await api.get_poses();
  if(!list.length){error('Позы не найдены',true);list=await api.choose_folder();}
  await firstPose(list);
}

document.querySelector('#prev').addEventListener('click',()=>requestStep(-1));
document.querySelector('#next').addEventListener('click',()=>requestStep(1));
document.querySelector('#choose').addEventListener('click',async()=>{
  const button=document.querySelector('#choose');button.disabled=true;
  try{await firstPose(await api.choose_folder());}catch(e){report(e);}finally{button.disabled=false;}
});
window.addEventListener('keydown',event=>{
  if(['ArrowRight','ArrowLeft','KeyD','KeyA','KeyF','F11','Escape'].includes(event.code))event.preventDefault();
  if(event.code==='Escape'){pending=0;finish();api?.quit();}
  else if(event.code==='F11'||event.code==='KeyF'){if(!event.repeat)api?.toggle_fullscreen();}
  else if(event.code==='ArrowRight'||event.code==='KeyD')requestStep(1);
  else if(event.code==='ArrowLeft'||event.code==='KeyA')requestStep(-1);
});
window.addEventListener('pointermove',event=>{wake();mouse={x:event.clientX/innerWidth*2-1,y:1-event.clientY/innerHeight*2,time:performance.now()};});
window.addEventListener('pointerdown',wake);
window.addEventListener('resize',resize);
window.addEventListener('focus',resume);window.addEventListener('blur',pause);
document.addEventListener('visibilitychange',()=>document.hidden?pause():document.hasFocus()&&resume());
window.addEventListener('contextmenu',event=>event.preventDefault());
window.addEventListener('error',event=>report(event.message));
window.addEventListener('unhandledrejection',event=>report(event.reason));
wake();

// Read-only diagnostics for local acceptance tests; no UI or external telemetry.
Object.defineProperty(window,'__poseStage',{get:()=>({
  index,file:poses[index]?.file,count:poses.length,transition:!!transition,pending,loading,active,
  fps,quality,firstPoseMs,renderedFrames,textureCount:cache?.entries.size,gpuTextures:renderer?.info.memory.textures,
  gpuGeometries:renderer?.info.memory.geometries,programs:renderer?.info.programs.length,
  box:current?.pose.box,pixelScale:current?.pixelScale,supportMode:current?.pose.support?.mode,
  maxContactRadius:current?.placement.maxContactRadius,
  supportCenter:current?[current.placement.mapping.centerX,current.placement.mapping.centerY]:null,
  podiumTop:.4,canvas:[canvas.width,canvas.height],error:errorPanel.hidden?null:document.querySelector('#message').textContent,
})});
start().catch(e=>{report(e);if(errorPanel.hidden)error('Не удалось запустить сцену. Подробности сохранены в журнале PoseStage.');});

export {requestStep,finish,renderFrame};
export {renderer,camera,figureCamera,current,cache,studio,post};
