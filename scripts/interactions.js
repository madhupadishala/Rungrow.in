const reducedMotion=window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const finePointer=window.matchMedia("(hover: hover) and (pointer: fine)").matches;

function initTilt(){
  if(reducedMotion||!finePointer)return;
  const elements=document.querySelectorAll("[data-tilt],.card");
  elements.forEach(element=>{
    element.dataset.tilt="";
    let frame=0;
    const reset=()=>{
      cancelAnimationFrame(frame);
      element.style.setProperty("--tilt-x","0deg");
      element.style.setProperty("--tilt-y","0deg");
      element.style.setProperty("--pointer-x","50%");
      element.style.setProperty("--pointer-y","50%");
      element.classList.remove("is-tilting");
    };
    element.addEventListener("pointermove",event=>{
      const rect=element.getBoundingClientRect();
      const x=(event.clientX-rect.left)/rect.width;
      const y=(event.clientY-rect.top)/rect.height;
      const depth=element.matches(".rg-3d-scene")?7:4.5;
      cancelAnimationFrame(frame);
      frame=requestAnimationFrame(()=>{
        element.style.setProperty("--tilt-x",((x-.5)*depth*2).toFixed(2)+"deg");
        element.style.setProperty("--tilt-y",((.5-y)*depth*2).toFixed(2)+"deg");
        element.style.setProperty("--pointer-x",(x*100).toFixed(1)+"%");
        element.style.setProperty("--pointer-y",(y*100).toFixed(1)+"%");
        element.classList.add("is-tilting");
      });
    });
    element.addEventListener("pointerleave",reset);
    element.addEventListener("blur",reset,true);
  });
}

function initReveal(){
  const targets=document.querySelectorAll(".section-head,.trust-item,.card,.workflow-step,.photo-hub-visual,.product-demo,.cta-panel");
  if(!("IntersectionObserver" in window)||reducedMotion){targets.forEach(el=>el.classList.add("rg-visible"));return;}
  const observer=new IntersectionObserver(entries=>{
    entries.forEach(entry=>{
      if(entry.isIntersecting){
        entry.target.classList.add("rg-visible");
        observer.unobserve(entry.target);
      }
    });
  },{threshold:.12,rootMargin:"0px 0px -36px 0px"});
  targets.forEach((el,index)=>{
    if(el.closest(".hero"))return;
    el.classList.add("rg-reveal");
    el.style.setProperty("--reveal-delay",Math.min(index%4,3)*65+"ms");
    observer.observe(el);
  });
}

function initScrollProgress(){
  const bar=document.createElement("div");
  bar.className="rg-scroll-progress";
  bar.setAttribute("aria-hidden","true");
  document.body.append(bar);
  let frame=0;
  const update=()=>{
    cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>{
      const max=document.documentElement.scrollHeight-window.innerHeight;
      const progress=max>0?window.scrollY/max:0;
      bar.style.transform="scaleX("+progress+")";
    });
  };
  window.addEventListener("scroll",update,{passive:true});
  window.addEventListener("resize",update);
  update();
}

function initHeroPointer(){
  if(reducedMotion||!finePointer)return;
  const hero=document.querySelector(".hero");
  if(!hero)return;
  hero.addEventListener("pointermove",event=>{
    const rect=hero.getBoundingClientRect();
    hero.style.setProperty("--hero-pointer-x",((event.clientX-rect.left)/rect.width*100).toFixed(1)+"%");
    hero.style.setProperty("--hero-pointer-y",((event.clientY-rect.top)/rect.height*100).toFixed(1)+"%");
  },{passive:true});
}

export function initInteractions(){
  initTilt();
  initReveal();
  initScrollProgress();
  initHeroPointer();
}
