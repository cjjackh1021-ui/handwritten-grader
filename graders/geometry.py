"""Affine maps use [a,b,c,d,e,f]: (x,y) -> (a*x+c*y+e,b*x+d*y+f)."""


def display_geometry(page):
    box = page.cropbox
    x, y, w, h = float(box.left), float(box.bottom), float(box.width), float(box.height)
    r = page.rotation % 360
    transforms = {0: (1,0,0,1,x,y), 90: (0,1,-1,0,x+w,y),
                  180: (-1,0,0,-1,x+w,y+h), 270: (0,-1,1,0,x,y+h)}
    if r not in transforms: raise ValueError("不支持非直角 PDF 旋转")
    return (h,w,transforms[r]) if r in (90,270) else (w,h,transforms[r])


def normalized_to_pdf(page):
    width,height,(a,b,c,d,e,f)=display_geometry(page)
    return [a*width,b*width,-c*height,-d*height,c*height+e,d*height+f]


def normalized_to_original(width,height,orientation=1):
    return {1:[width,0,0,height,0,0],2:[-width,0,0,height,width,0],
            3:[-width,0,0,-height,width,height],4:[width,0,0,-height,0,height],
            5:[0,height,width,0,0,0],6:[0,-height,width,0,0,height],
            7:[0,-height,-width,0,width,height],8:[0,height,-width,0,width,0]}.get(orientation,[width,0,0,height,0,0])
