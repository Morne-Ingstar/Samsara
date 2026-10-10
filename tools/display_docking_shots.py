"""Hermetic real-widget placement evidence on a drawn desktop, never the live app."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compact-only', action='store_true')
    args=parser.parse_args(); output=args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    home=output.parent/'picture_home'; home.mkdir(exist_ok=True)
    for key in ('HOME','USERPROFILE','SAMSARA_HOME_DIR','APPDATA','LOCALAPPDATA','TEMP','TMP','TMPDIR'):
        os.environ[key]=str(home)
    os.environ.update(QT_QPA_PLATFORM='offscreen',QT_QPA_FONTDIR='C:/Windows/Fonts',PYTHONDONTWRITEBYTECODE='1')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from types import SimpleNamespace
    from unittest.mock import patch
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QPoint, QRect, QCoreApplication, QEvent, QTimer
    from PySide6.QtGui import QImage, QPainter, QColor, QFont
    from PySide6.QtWidgets import QApplication
    from samsara.ui import qt_runtime
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import (CaptureState,DocumentState,Notice,NoticeKind,
        PresentationState,SurfaceView,VisibleForm)
    from samsara.live_surface.placement import Edge,Placement,Rect,Screen,placement_record
    from samsara.ui.listening_indicator import ListeningIndicator
    import shiboken6
    done=threading.Event(); errors=[]; manifest=[]
    def capture(widget,name,scale,area):
        b=Rect(widget.x(),widget.y(),widget.width(),widget.height())
        assert area.contains(b),(name,b)
        canvas=QImage(1920,1080,QImage.Format.Format_ARGB32);canvas.fill(QColor('#263647'))
        painter=QPainter(canvas)
        try:
            painter.fillRect(0,1040,1920,40,QColor('#101820'))
            painter.setPen(QColor('#ffffff'));painter.setFont(QFont('Segoe UI',14))
            painter.drawText(12,1067,'Start');painter.drawText(420,80,name.replace('_',' '))
            painter.drawText(420,108,'Drawn desktop / real offscreen widget / synthetic state')
            painter.scale(scale,scale);painter.translate(widget.x(),widget.y())
            widget.render(painter,QPoint())
        finally:painter.end()
        canvas.save(str(output/(name+'.png')))
        manifest.append(dict(name=name+'.png',display='Compact' if name.startswith('Compact') else 'Full',
            scale=scale,bounds=[b.x,b.y,b.width,b.height],bottom_physical=round(b.bottom*scale)))
    def render():
        try:
            for scale in (1.,1.5):
                area=Rect(0,0,1920/scale,1040/scale)
                screen=Screen('synthetic',area,True)
                choices=[('bottom_left',Placement(Edge.BOTTOM,0))]
                if scale==1:
                    choices += [('top_right',Placement(Edge.TOP,1)),('left_middle',Placement(Edge.LEFT,.5)),
                        ('free_middle',Placement(Edge.FREE,free_cx=.45,free_cy=.4))]
                for location,placement in choices:
                    if args.compact_only: continue
                    app=SimpleNamespace(config={'ui':{'live_surface':{'enabled':True,'placement':{
                        'preferred_monitor':screen.id,'monitors':{screen.id:placement_record(placement)}}}}})
                    c=LiveSurfaceController(app);c._screens=lambda:[screen];c.start()
                    w=c.widget;w.setAttribute(__import__('PySide6.QtCore',fromlist=['Qt']).Qt.WidgetAttribute.WA_DontShowOnScreen,True)
                    states=[('idle',VisibleForm.STATUS,CaptureState.OFF,'',NoticeKind.NONE),
                        ('listening',VisibleForm.STATUS,CaptureState.HANDS_FREE_LISTENING,'',NoticeKind.NONE),
                        ('draft',VisibleForm.LIVE,CaptureState.RECORDING,'Synthetic first sentence. Synthetic second sentence. Synthetic third sentence. '*2,NoticeKind.NONE),
                        ('review',VisibleForm.REVIEW,CaptureState.OFF,'Synthetic review text. '*30,NoticeKind.NONE),
                        ('message',VisibleForm.STATUS,CaptureState.OFF,'',NoticeKind.SUCCESS)]
                    if location!='bottom_left':states=[states[0],states[2]]
                    for name,form,capture_state,text,notice in states:
                        v=SurfaceView(form,capture_state,None,DocumentState.EDITABLE_DRAFT if text else DocumentState.EMPTY,
                            PresentationState.COMPACT,Notice(notice,'Synthetic completed action' if notice is not NoticeKind.NONE else '',None),
                            'synthetic',1,text,'',True)
                        w.refresh(v,animate=False);c._place_widget()
                        capture(w,f'Full_{int(scale*100)}_{location}_{name}',scale,area)
                    c.close()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                # Compact uses the same work-area docking under a synthetic monitor.
                fake=SimpleNamespace(availableGeometry=lambda:QRect(0,0,round(area.width),round(area.height)),name=lambda:'synthetic')
                with patch.object(QApplication,'primaryScreen',return_value=fake), patch.object(QApplication,'screens',return_value=[fake]):
                    compact_states = [('bottom_left',name) for name in ('idle','listening','draft','message')] if scale == 1 else [('bottom_left','message')]
                    if scale == 1:
                        compact_states += [('top_right','idle'),('left_middle','idle'),('free_middle','idle')]
                    for location,name in compact_states:
                        w=ListeningIndicator()
                        if location == 'left_middle':
                            w.set_custom_position('synthetic', .1, .5, edge='left', t=.5)
                        elif location == 'free_middle':
                            w.set_custom_position('synthetic', .45, .4)
                        else: w.set_position(location.replace('_','-'))
                        w.show()
                        if name in ('listening','draft'):w.set_listening(True)
                        if name == 'draft':w.show_outcome('Synthetic three-line draft','pending',100000)
                        if name in ('review','message'):w.show_outcome('Synthetic completed action','success',100000)
                        w._reposition()
                        capture(w,f'Compact_{int(scale*100)}_{location}_{name}',scale,area)
                        # hide() can keep a protected chip alive; destroy() stops
                        # all six timers. Delete on the owning Qt thread before
                        # its loop exits, rather than at Python interpreter exit.
                        w.destroy(); shiboken6.delete(w)
            (output/('compact_manifest.json' if args.compact_only else 'manifest.json')).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
            for offset in range(0,len(manifest),6):
                sheet=Image.new('RGB',(1920,1080),'#777777');draw=ImageDraw.Draw(sheet)
                for index,item in enumerate(manifest[offset:offset+6]):
                    with Image.open(output/item['name']) as im:
                        im.thumbnail((640,500));x=index%3*640;y=index//3*540
                        sheet.paste(im.convert('RGB'),(x,y+30));draw.text((x+8,y+8),item['name'],fill='white')
                sheet.save(output/f'compact_sheet_{offset//6:02}.jpg')
        except BaseException as error:errors.append(error)
        finally:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            survivors = QApplication.topLevelWidgets()
            (output/'teardown.json').write_text(json.dumps({'remaining_widgets': len(survivors),
                'active_widget_timers': sum(t.isActive() for w in survivors for t in w.findChildren(QTimer))}),encoding='utf-8')
            for survivor in survivors:
                shiboken6.delete(survivor)
            done.set()
    # This standalone process runs the central runtime's loop on its main
    # thread. Qt's application must also be destroyed there, rather than
    # surviving a secondary Qt thread until interpreter teardown on Windows.
    def orchestrate():
        if not qt_runtime._ready.wait(10):
            errors.append(RuntimeError('Qt startup timed out')); return
        qt_runtime.post(render)
        if not done.wait(60):errors.append(RuntimeError('Picture render timed out'))
        qt_runtime.shutdown()
    worker = threading.Thread(target=orchestrate, name='picture-orchestrator')
    worker.start()
    qt_runtime._run_loop()
    worker.join(5)
    shiboken6.delete(QApplication.instance())
    qt_runtime._app = None
    if errors:raise errors[0]
    print(f'Rendered {len(manifest)} real-widget desktop pictures')


if __name__=='__main__':main()
