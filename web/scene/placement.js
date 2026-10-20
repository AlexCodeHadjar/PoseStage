import * as THREE from 'three';
import {TOP,RADIUS} from './stage.js';

export const SUPPORT_RADIUS=RADIUS-.18;
const cache=new WeakMap(),supports=new WeakMap(),point=new THREE.Vector3();

// Invert the projection of the actual horizontal podium top. Source drawings
// already contain perspective, so their support pixels have different depths.
export function groundMapping(camera) {
  const [width,height]=camera.userData.viewport||[1920,1080];
  const signature=[width,height,...camera.matrixWorld.elements,...camera.projectionMatrix.elements];
  const previous=cache.get(camera);
  if(previous&&signature.every((v,i)=>v===previous.signature[i]))return previous;
  const vp=new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix,camera.matrixWorldInverse),e=vp.elements;
  const inverse=new THREE.Matrix3().set(
    e[0],e[8],e[4]*TOP+e[12],
    e[1],e[9],e[5]*TOP+e[13],
    e[3],e[11],e[7]*TOP+e[15]).invert().elements;
  let left=Infinity,right=-Infinity,bottom=Infinity,top=-Infinity;
  for(let i=0;i<128;i++) {
    const a=i*Math.PI/64;
    point.set(Math.cos(a)*RADIUS,TOP,Math.sin(a)*RADIUS).project(camera);
    left=Math.min(left,point.x*width/2);right=Math.max(right,point.x*width/2);
    bottom=Math.min(bottom,point.y*height/2);top=Math.max(top,point.y*height/2);
  }
  const mapping={signature,width,height,inverse,centerX:(left+right)/2,centerY:(bottom+top)/2,diameter:right-left};
  cache.set(camera,mapping);return mapping;
}

export function toGround(mapping,x,y,target=new THREE.Vector3()) {
  const u=x*2/mapping.width,v=y*2/mapping.height,e=mapping.inverse;
  const w=e[2]*u+e[5]*v+e[8];
  return target.set((e[0]*u+e[3]*v+e[6])/w,TOP,(e[1]*u+e[4]*v+e[7])/w);
}

function supportData(pose) {
  if(supports.has(pose))return supports.get(pose);
  const [x1,y1,x2,y2]=pose.box,bh=y2-y1;
  const threshold=y1+(pose.support?.cut??.93)*bh;
  let contacts=(pose.support?.profile||[[(x1+x2)/2,y2]]).filter(p=>p[1]>=threshold);
  if(!contacts.length)contacts=[[(x1+x2)/2,y2]];
  const cx=(Math.min(...contacts.map(p=>p[0]))+Math.max(...contacts.map(p=>p[0])))/2;
  const cy=(Math.min(...contacts.map(p=>p[1]))+Math.max(...contacts.map(p=>p[1])))/2;
  const data={threshold,contacts,cx,cy};supports.set(pose,data);return data;
}

export function fitPlacement(pose,mapping) {
  const [x1,y1,x2,y2]=pose.box,bh=y2-y1;
  const {threshold,contacts,cx,cy}=supportData(pose);
  let high=Math.min(.61*mapping.height/bh,.90*mapping.diameter/(2*Math.max(cx-x1,x2-cx)));
  high=Math.min(high,(mapping.height*.43-mapping.centerY)/(cy-y1),(mapping.centerY+mapping.height*.445)/(y2-cy));
  const temp=new THREE.Vector3();
  const radiusAt=s=>{
    let max=0;
    for(const [x,y] of contacts) {
      toGround(mapping,mapping.centerX+(x-cx)*s,mapping.centerY+(cy-y)*s,temp);
      max=Math.max(max,Math.hypot(temp.x,temp.z));
    }
    return max;
  };
  let lo=0;
  if(radiusAt(high)<=SUPPORT_RADIUS)lo=high;
  else for(let i=0;i<22;i++) {const mid=(lo+high)/2;if(radiusAt(mid)<=SUPPORT_RADIUS)lo=mid;else high=mid;}
  return {scale:lo,cx,cy,threshold,contacts,maxContactRadius:radiusAt(lo),mapping,
    x:mapping.centerX+((x1+x2)/2-cx)*lo,y:mapping.centerY+(cy-(y1+y2)/2)*lo};
}

export function baselineAt(pose,x,threshold) {
  const p=pose.support?.profile;
  if(!p?.length)return pose.box[3];
  let lo=0,hi=p.length-1;
  while(hi-lo>1) {const mid=(lo+hi)>>1;if(p[mid][0]<x)lo=mid;else hi=mid;}
  const t=THREE.MathUtils.clamp((x-p[lo][0])/Math.max(.001,p[hi][0]-p[lo][0]),0,1);
  return Math.max(threshold,THREE.MathUtils.lerp(p[lo][1],p[hi][1],t));
}
