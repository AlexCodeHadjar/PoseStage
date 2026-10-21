// Frozen copy of 3c5ca12 for refactor-equivalence tests; regenerate or delete when placement changes intentionally.
import * as THREE from 'three';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';

export const RADIUS=2, TOP=.4, ANCHOR_Z=-.12;

function softDisc(opacity, width, depth, y) {
  const canvas=document.createElement('canvas'); canvas.width=canvas.height=128;
  const c=canvas.getContext('2d'), g=c.createRadialGradient(64,64,4,64,64,64);
  g.addColorStop(0,`rgba(0,0,0,${opacity})`); g.addColorStop(.4,`rgba(0,0,0,${opacity*.6})`); g.addColorStop(1,'rgba(0,0,0,0)');
  c.fillStyle=g;c.fillRect(0,0,128,128);
  const map=new THREE.CanvasTexture(canvas);
  const mesh=new THREE.Mesh(new THREE.PlaneGeometry(width,depth),new THREE.MeshBasicMaterial({map,transparent:true,depthWrite:false,toneMapped:false}));
  mesh.rotation.x=-Math.PI/2;mesh.position.y=y;return mesh;
}

export function createStage(renderer) {
  const scene=new THREE.Scene();
  scene.background=new THREE.Color('#292c31');
  scene.fog=new THREE.Fog('#292c31',11,28);
  const pmrem=new THREE.PMREMGenerator(renderer), room=new RoomEnvironment();
  const env=pmrem.fromScene(room,.04); scene.environment=env.texture;
  room.dispose();pmrem.dispose();scene.environmentIntensity=.5;

  // Fog shares the background color, avoiding a visible horizon line.
  const floor=new THREE.Mesh(new THREE.PlaneGeometry(200,200),new THREE.MeshStandardMaterial({color:'#030405',roughness:.94,metalness:0,envMapIntensity:.06}));
  floor.rotation.x=-Math.PI/2;floor.receiveShadow=true;scene.add(floor);
  scene.add(softDisc(.5,6.3,5.5,.006));

  const podium=new THREE.Group();scene.add(podium);
  const profile=[new THREE.Vector2(0,.006),new THREE.Vector2(1.94,.006)];
  const bevel=.055;
  for(let i=0;i<=8;i++) {const a=-Math.PI/2+i*Math.PI/16;profile.push(new THREE.Vector2(RADIUS-bevel+bevel*Math.cos(a),.006+bevel+bevel*Math.sin(a)));}
  profile.push(new THREE.Vector2(RADIUS,TOP-bevel));
  for(let i=0;i<=8;i++) {const a=i*Math.PI/16;profile.push(new THREE.Vector2(RADIUS-bevel+bevel*Math.cos(a),TOP-bevel+bevel*Math.sin(a)));}
  profile.push(new THREE.Vector2(0,TOP));
  const ceramic=new THREE.MeshPhysicalMaterial({color:'#f4f4f2',roughness:.29,metalness:0,clearcoat:.85,clearcoatRoughness:.14,envMapIntensity:.8});
  const body=new THREE.Mesh(new THREE.LatheGeometry(profile,160),ceramic);body.castShadow=true;body.receiveShadow=true;podium.add(body);
  const seamMaterial=new THREE.LineBasicMaterial({color:'#a8a9aa',transparent:true,opacity:.22});
  for(let i=0;i<8;i++) {
    const a=i*Math.PI/4, pts=[.055,.34,.378].map(y=>new THREE.Vector3(Math.sin(a)*(RADIUS+.001),y,Math.cos(a)*(RADIUS+.001)));
    podium.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),seamMaterial));
  }
  const contact=softDisc(.29,.55,.32,TOP+.002);contact.position.z=ANCHOR_Z;scene.add(contact);

  const key=new THREE.DirectionalLight('#fff7ec',2.8);key.position.set(-3.5,8,4);key.castShadow=true;
  Object.assign(key.shadow.camera,{left:-5,right:5,top:6,bottom:-3,near:.1,far:25});
  key.shadow.mapSize.set(2048,2048);key.shadow.bias=-.00012;key.shadow.normalBias=.012;key.shadow.radius=4;
  key.target.position.set(0,.7,0);scene.add(key,key.target);
  const fill=new THREE.DirectionalLight('#e4ecff',.9);fill.position.set(5,3,5);scene.add(fill);
  const rim=new THREE.DirectionalLight('#ffffff',2.3);rim.position.set(1,6,-4);scene.add(rim);
  scene.add(new THREE.AmbientLight('#cbd3e0',.15));
  return {scene,podium,contact,env};
}

export function frameCamera(camera,width,height) {
  camera.userData.viewport=[width,height];
  camera.aspect=width/height;
  const tangent=Math.tan(THREE.MathUtils.degToRad(camera.fov/2));
  const distance=RADIUS*2/(.48*2*tangent*camera.aspect);
  const targetY=TOP+2*distance*tangent*.265;
  const target=new THREE.Vector3(0,targetY,0);
  camera.position.set(0,targetY+distance*Math.sin(.112),distance*Math.cos(.112));
  camera.lookAt(target);camera.updateProjectionMatrix();camera.updateMatrixWorld();
  return {distance,target,base:camera.position.clone()};
}
