import * as THREE from 'three';
import {dissolveMaterial} from '../shaders/dissolve.js';
import {groundMapping,fitPlacement,toGround,baselineAt} from './placement.js';

function cropUV(geometry,pose) {
  const [x1,y1,x2,y2]=pose.box,uv=geometry.attributes.uv;
  for(let i=0;i<uv.count;i++)uv.setXY(i,(x1+uv.getX(i)*(x2-x1))/pose.w,1-y2/pose.h+uv.getY(i)*(y2-y1)/pose.h);
}

export class Figure {
  constructor(pose,texture,stageScene,overlayScene) {
    this.pose=pose;this.texture=texture;
    this.uniforms={uReveal:{value:1},uErase:{value:0},uBounds:{value:new THREE.Vector2(1-pose.box[3]/pose.h,1-pose.box[1]/pose.h)}};
    this.geometry=new THREE.PlaneGeometry(1,1);cropUV(this.geometry,pose);
    const material=new THREE.MeshBasicMaterial({map:texture,alphaTest:.5,side:THREE.DoubleSide,toneMapped:false,alphaToCoverage:true});
    dissolveMaterial(material,this.uniforms,true);
    this.mesh=new THREE.Mesh(this.geometry,material);
    this.group=new THREE.Group();this.group.add(this.mesh);overlayScene.add(this.group);
    // Only the invisible shadow geometry follows the ground depths. The visible
    // PNG retains its original proportions, colors, and straight image plane.
    this.shadowGeometry=new THREE.PlaneGeometry(1,1,128,1);cropUV(this.shadowGeometry,pose);
    const ghost=new THREE.MeshBasicMaterial({map:texture,alphaTest:.5,colorWrite:false,depthWrite:false,side:THREE.DoubleSide});
    ghost.onBeforeCompile=shader=>{shader.fragmentShader='void main(){gl_FragColor=vec4(0.0);}';};
    ghost.customProgramCacheKey=()=> 'pose-shadow-only-caster-v1';
    this.shadow=new THREE.Mesh(this.shadowGeometry,ghost);this.shadow.castShadow=true;this.shadow.frustumCulled=false;
    this.shadow.customDepthMaterial=new THREE.MeshDepthMaterial({map:texture,alphaTest:.5,depthPacking:THREE.RGBADepthPacking,side:THREE.DoubleSide});
    dissolveMaterial(this.shadow.customDepthMaterial,this.uniforms,false);
    stageScene.add(this.shadow);
    this.ground=new THREE.Vector3();
  }
  fit(camera) {
    const mappingForCamera=groundMapping(camera);
    if(this.placement?.mapping===mappingForCamera)return;
    const placement=this.placement=fitPlacement(this.pose,mappingForCamera);
    const {scale,x,y,threshold,mapping,cx,cy}=placement;
    const [x1,y1,x2,y2]=this.pose.box;
    this.pixelScale=scale;
    this.mesh.scale.set((x2-x1)*scale,(y2-y1)*scale,1);this.group.position.set(x,y,0);
    const positions=this.shadowGeometry.attributes.position,uv=this.shadowGeometry.attributes.uv;
    const projection=camera.projectionMatrix.elements,world=camera.matrixWorld.elements;
    for(let i=0;i<positions.count;i++) {
      const sourceX=uv.getX(i)*this.pose.w,sourceY=(1-uv.getY(i))*this.pose.h;
      const px=mapping.centerX+(sourceX-cx)*scale,py=mapping.centerY+(cy-sourceY)*scale;
      const gy=mapping.centerY+(cy-baselineAt(this.pose,sourceX,threshold))*scale;
      toGround(mapping,px,gy,this.ground).applyMatrix4(camera.matrixWorldInverse);
      const depth=-this.ground.z;
      const vx=(px*2/mapping.width+projection[8])*depth/projection[0];
      const vy=(py*2/mapping.height+projection[9])*depth/projection[5];
      positions.setXYZ(i,
        world[0]*vx+world[4]*vy-world[8]*depth+world[12],
        world[1]*vx+world[5]*vy-world[9]*depth+world[13],
        world[2]*vx+world[6]*vy-world[10]*depth+world[14]);
    }
    positions.needsUpdate=true;
  }
  face(camera) {this.fit(camera);}
  sourceToScreen(x,y,target=new THREE.Vector3()) {
    const p=this.placement;
    return target.set(p.mapping.centerX+(x-p.cx)*p.scale,p.mapping.centerY+(p.cy-y)*p.scale,0);
  }
  dispose() {
    this.group.removeFromParent();this.shadow.removeFromParent();this.geometry.dispose();this.shadowGeometry.dispose();
    this.mesh.material.dispose();this.shadow.material.dispose();this.shadow.customDepthMaterial.dispose();
  }
}
