import re
import click
from typing import List, Dict

from .mpd import MPD
from .handler import xml_handler
from .childs.adaptationset import AdaptationSet
from .childs.role import Role
from .childs.baseurl import BaseURL
from .childs.contentprotection import ContentProtection
from .childs.period import Period
from .childs.representation import Representation
from .childs.s import S
from .childs.segmenttemplate import SegmentTemplate
from .childs.segmenttimeline import SegmentTimeline

from .stream import DASHStream
from ..base import BaseParser
from .key import DASHKey
from XstreamDL_CLI.cmdargs import CmdArgs


class DASHParser(BaseParser):
    def __init__(self, args: CmdArgs, uri_type: str):
        super(DASHParser, self).__init__(args, uri_type)
        self.suffix = '.mpd'

    def parse(self, uri: str, content: str) -> List[DASHStream]:
        uris = self.parse_uri(uri)
        if uris is None:
            click.secho(f'parse {uri} failed')
            return []
        name, home_url, base_url = uris
        self.dump_content(name, content, self.suffix)
        # 解析转换内容为期望的对象
        mpd = xml_handler(content)
        # 检查有没有baseurl
        base_urls = mpd.find('BaseURL') # type: List[BaseURL]
        if len(base_urls) > 0:
            base_url = base_urls[0].innertext
            uris = [name, home_url, base_url]
        return self.walk_period(mpd, uris)

    def walk_period(self, mpd: MPD, uris: list):
        periods = mpd.find('Period')  # type: List[Period]
        # 根据Period数量处理时长参数
        if len(periods) == 1 and periods[0].duration is None:
            # 当只存在一条流 且当前Period没有duration属性
            # 则使用mediaPresentationDuration作为当前Period的时长
            if hasattr(mpd, 'mediaPresentationDuration'):
                periods[0].duration = mpd.mediaPresentationDuration
            else:
                periods[0].duration = 0.0
        # 遍历处理periods
        streams = []
        for period in periods:
            _streams = self.walk_adaptationset(period, len(streams), uris)
            streams.extend(_streams)
        # 处理掉末尾的空分段
        for stream in streams:
            if stream.segments[-1].url == '':
                _ = stream.segments.pop(-1)
        if len(periods) == 1 or self.args.split:
            return streams
        # 合并流
        skey_stream = {} # type: Dict[str, DASHStream]
        for stream in streams:
            if stream.skey in skey_stream:
                skey_stream[stream.skey].update(stream)
            else:
                skey_stream[stream.skey] = stream
        streams = list(skey_stream.values())
        return streams

    def walk_adaptationset(self, period: Period, sindex: int, uris: list):
        adaptationsets = period.find('AdaptationSet')  # type: List[AdaptationSet]
        streams = []
        for adaptationset in adaptationsets:
            if adaptationset.mimeType == 'image/jpeg':
                continue
            _streams = self.walk_representation(adaptationset, period, sindex + len(streams), uris)
            streams.extend(_streams)
        return streams

    def walk_representation(self, adaptationset: AdaptationSet, period: Period, sindex: int, uris: list):
        '''
        每一个<Representation></Representation>都对应轨道的一/整段
        '''
        name, home_url, base_url = uris
        representations = adaptationset.find('Representation') # type: List[Representation]
        segmenttemplates = adaptationset.find('SegmentTemplate') # type: List[SegmentTemplate]
        streams = []
        for representation in representations:
            stream = DASHStream(sindex, name, home_url, base_url, self.args.save_dir)
            sindex += 1
            self.walk_contentprotection(representation, stream)
            # 给流设置属性
            stream.set_skey(adaptationset.id, representation.id)
            stream.set_lang(adaptationset.lang)
            stream.set_bandwidth(representation.bandwidth)
            if representation.codecs is None:
                stream.set_codecs(adaptationset.codecs)
            else:
                stream.set_codecs(representation.codecs)
            if representation.mimeType is None:
                stream.set_stream_type(adaptationset.mimeType)
            else:
                stream.set_stream_type(representation.mimeType)
            if representation.width is None or representation.height is None:
                stream.set_resolution(adaptationset.width, adaptationset.height)
            else:
                stream.set_resolution(representation.width, representation.height)
            # 针对字幕直链类型
            Roles = adaptationset.find('Role') # type: List[Role]
            BaseURLs = representation.find('BaseURL') # type: List[BaseURL]
            if len(BaseURLs) == 1:
                if len(Roles) == 1 and Roles[0].value == 'subtitle':
                    stream.set_subtitle_url(BaseURLs[0].innertext)
                    streams.append(stream)
                    continue
                stream.fix_base_url(BaseURLs[0].innertext)
                if len(segmenttemplates) == 0 and len(representation.find('SegmentTimeline')) == 0:
                    stream.base2url(period.duration)
                    streams.append(stream)
                    continue
            # 针对视频音频流处理 分情况生成链接
            if len(segmenttemplates) == 0:
                self.walk_segmenttemplate(representation, period, stream)
            elif len(segmenttemplates) == 1 and len(segmenttemplates[0].find('SegmentTimeline')) == 1:
                self.walk_segmenttimeline(segmenttemplates[0], representation, stream)
            else:
                # SegmentTemplate 和多个 Representation 在同一级
                # 那么 SegmentTemplate 的时长参数等就是多个 Representation 的参数
                # 同一级的时候 只有一个 SegmentTemplate
                self.generate_v1(period, representation.id, segmenttemplates[0], stream)
            streams.append(stream)
        return streams

    def walk_contentprotection(self, representation: Representation, stream: DASHStream):
        ''' 流的加密方案 '''
        contentprotections = representation.find('ContentProtection') # type: List[ContentProtection]
        for contentprotection in contentprotections:
            # DASH流的解密通常是合并完整后一次解密
            # 不适宜每个分段单独解密
            # 那么这里就不用给每个分段设置解密key了
            # 而且往往key不好拿到 所以这里仅仅做一个存储
            stream.append_key(DASHKey(contentprotection))

    def walk_segmenttemplate(self, representation: Representation, period: Period, stream: DASHStream):
        segmenttemplates = representation.find('SegmentTemplate') # type: List[SegmentTemplate]
        # segmenttimelines = representation.find('SegmentTimeline') # type: List[SegmentTimeline]
        if len(segmenttemplates) != 1:
            # 正常情况下 这里应该只有一个SegmentTemplate
            # 没有就无法计算分段 则跳过
            # 不止一个可能是没见过的类型 提醒上报
            if len(segmenttemplates) > 1:
                click.secho('please report this DASH content.')
            else:
                click.secho('stream has no SegmentTemplate between Representation tag.')
            return
        if len(segmenttemplates[0].find('SegmentTimeline')) == 0:
            self.generate_v1(period, representation.id, segmenttemplates[0], stream)
            return
        self.walk_segmenttimeline(segmenttemplates[0], representation, stream)

    def walk_segmenttimeline(self, segmenttemplate: SegmentTemplate, representation: Representation, stream: DASHStream):
        segmenttimelines = segmenttemplate.find('SegmentTimeline') # type: List[SegmentTimeline]
        if len(segmenttimelines) != 1:
            if len(segmenttimelines) > 1:
                click.secho('please report this DASH content.')
            else:
                click.secho('stream has no SegmentTimeline between SegmentTemplate tag.')
            return
        self.walk_s(segmenttimelines[0], segmenttemplate, representation, stream)

    def walk_s(self, segmenttimeline: SegmentTimeline, st: SegmentTemplate, representation: Representation, stream: DASHStream):
        init_url = st.get_url()
        if init_url is not None:
            if '$RepresentationID$' in init_url:
                init_url = init_url.replace('$RepresentationID$', representation.id)
            if '$Bandwidth$' in init_url:
                init_url = init_url.replace('$Bandwidth$', str(representation.bandwidth))
            if re.match('.*?as=audio_(.*?)\)', init_url):
                _lang = re.match('.*?as=audio_(.*?)\)', init_url).groups()[0]
                stream.set_lang(_lang)
            stream.set_init_url(init_url)
        else:
            # 这种情况可能是因为流是字幕
            pass
        ss = segmenttimeline.find('S') # type: List[S]
        time_offset = st.presentationTimeOffset
        start_number = st.startNumber
        for s in ss:
            interval = s.d / st.timescale
            for number in range(s.r):
                media_url = st.get_media_url()
                if '$Bandwidth$' in media_url:
                    media_url = media_url.replace('$Bandwidth$', str(representation.bandwidth))
                if '$Number$' in media_url:
                    media_url = media_url.replace('$Number$', str(start_number))
                    start_number += 1
                if re.match('.*?\$Number%(\d+)d\$', media_url):
                    length = re.match('.*?\$Number%(\d+)d\$', media_url).groups()[0]
                    old = f'$Number%{length}d$'
                    media_url = media_url.replace(old, f'{start_number:>int(length)}')
                    start_number += 1
                if '$RepresentationID$' in media_url:
                    media_url = media_url.replace('$RepresentationID$', representation.id)
                if '$Time$' in media_url:
                    media_url = media_url.replace('$Time$', str(time_offset))
                    time_offset += s.d
                stream.set_segment_duration(interval)
                stream.set_media_url(media_url)

    def generate_v1(self, period: Period, rid: str, st: SegmentTemplate, stream: DASHStream):
        init_url = st.get_url()
        if '$RepresentationID$' in init_url:
            init_url = init_url.replace('$RepresentationID$', rid)
        stream.set_init_url(init_url)
        interval = float(int(st.duration) / int(st.timescale))
        repeat = int(round(period.duration / interval))
        for number in range(int(st.startNumber), repeat + int(st.startNumber)):
            media_url = st.get_media_url()
            if '$Number$' in media_url:
                media_url = media_url.replace('$Number$', str(number))
            if re.match('.*?\$Number%(\d+)d\$', media_url):
                length = re.match('.*?\$Number%(\d+)d\$', media_url).groups()[0]
                old = f'$Number%{length}d$'
                media_url = media_url.replace(old, f'{number:0>{int(length)}}')
            if '$RepresentationID$' in media_url:
                media_url = media_url.replace('$RepresentationID$', rid)
            stream.set_media_url(media_url)
        stream.set_segments_duration(interval)